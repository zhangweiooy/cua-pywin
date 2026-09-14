"""Development-only adapters for the two historical icon-semantic PoCs."""

from __future__ import annotations
import json
import os
import re
from pathlib import Path
from typing import Any, Optional, Sequence

import numpy as np
import onnxruntime as ort
from PIL import Image
from computer_server.services.icon_caption_service import (
    IconCaptionService as ProductionIconCaptionService,
    IconCaption,
    GENERATION_PROMPT,
    BUNDLED_FLORENCE2_MODEL_DIR,
    normalize_caption,
)

QUASIVISION_IMAGE_SIZE = 96
QUASIVISION_CONFIDENCE_PROVENANCE = "quasivision_softmax"
FLORENCE2_INT8_CONFIDENCE_PROVENANCE = "florence2_onnx_greedy_generation"


def normalize_classifier_label(label: str) -> Optional[str]:
    cleaned = re.sub(r"[_\-]+", " ", str(label or "")).strip()
    return None if cleaned.casefold() == "unknown" else cleaned[:160] or None


class PocIconCaptionService(ProductionIconCaptionService):
    def __init__(self, *, confidence_threshold: float = 0.4, **kwargs: Any) -> None:
        self.confidence_threshold = confidence_threshold
        self._session = None
        self._session_input_name = None
        self._session_output_name = None
        self._labels = []
        self._decoder_start_token_id = 2
        self._eos_token_id = 1
        super().__init__(**kwargs)

    def _default_processor_path(self) -> str:
        if self.backend == "quasivision":
            return os.environ.get(
                "CUA_QUASIVISION_LABELS",
                str(Path(self.model_name_or_path).with_name("labels.json")),
            )
        if self.backend == "florence2_int8":
            return str(BUNDLED_FLORENCE2_MODEL_DIR)
        return super()._default_processor_path()

    def _load_backend(self) -> None:
        if self.backend == "quasivision":
            self._load_quasivision()
        elif self.backend == "florence2_int8":
            self._load_florence2_int8()
        else:
            super()._load_backend()

    def _caption_icons_ready(self, images):
        if self.backend == "quasivision":
            return self._caption_quasivision(images)
        if self.backend == "florence2_int8":
            return self._caption_florence2_int8(images)
        return super()._caption_icons_ready(images)

    def _load_quasivision(self) -> None:
        model_path = self._require_local_file(
            self.model_name_or_path, "quasivision icon classifier"
        )
        labels_path = Path(self.processor_name_or_path).expanduser()
        labels_path = self._require_local_file(str(labels_path), "quasivision labels")
        with labels_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict):
            raise ValueError("quasivision labels.json must contain an object")
        indexed = sorted(
            ((int(key), value) for key, value in payload.items()),
            key=lambda item: item[0],
        )
        if [index for index, _ in indexed] != list(range(len(indexed))):
            raise ValueError("quasivision labels.json ids must be contiguous from zero")
        if not all(isinstance(value, str) and value for _, value in indexed):
            raise ValueError("quasivision labels.json values must be non-empty strings")

        session = self._create_onnx_session(model_path)
        inputs = session.get_inputs()
        outputs = session.get_outputs()
        if len(inputs) != 1 or len(outputs) != 1:
            raise ValueError(
                "quasivision classifier must expose one input and one output"
            )
        input_shape = list(inputs[0].shape)
        output_shape = list(outputs[0].shape)
        if input_shape[1:] != [1, QUASIVISION_IMAGE_SIZE, QUASIVISION_IMAGE_SIZE]:
            raise ValueError(f"unexpected quasivision input shape: {input_shape}")
        if output_shape[-1] != len(indexed):
            raise ValueError(
                f"quasivision output/labels mismatch: {output_shape[-1]} != {len(indexed)}"
            )
        self._session = session
        self._session_input_name = inputs[0].name
        self._session_output_name = outputs[0].name
        self._labels = [value for _, value in indexed]

    def _load_florence2_int8(self) -> None:
        model_path = self._require_local_file(
            self.model_name_or_path, "Florence-2 INT8 ONNX model"
        )
        processor_path = self._require_local_directory(
            self.processor_name_or_path, "Florence-2 processor"
        )
        from transformers import AutoProcessor

        session = self._create_onnx_session(model_path)
        input_names = {item.name for item in session.get_inputs()}
        required_inputs = {"input_ids", "pixel_values", "decoder_input_ids"}
        if not required_inputs.issubset(input_names):
            raise ValueError(
                "Florence-2 INT8 ONNX model is missing inputs: "
                f"{sorted(required_inputs - input_names)}"
            )
        processor = AutoProcessor.from_pretrained(
            str(processor_path),
            local_files_only=True,
            trust_remote_code=True,
        )
        with (processor_path / "generation_config.json").open(
            "r", encoding="utf-8"
        ) as handle:
            generation_config = json.load(handle)
        self._decoder_start_token_id = int(
            generation_config.get("decoder_start_token_id", 2)
        )
        self._eos_token_id = int(generation_config.get("eos_token_id", 1))
        self._session = session
        self._session_input_name = "input_ids"
        self._session_output_name = session.get_outputs()[0].name
        self._processor = processor

    def _caption_quasivision(
        self,
        images: Sequence[Image.Image],
    ) -> list[Optional[IconCaption]]:
        if self._session is None or not self._labels:
            return [None] * len(images)
        batches = [
            np.stack(
                [
                    self._quasivision_preprocess(image)
                    for image in images[start : start + self.batch_size]
                ]
            )
            for start in range(0, len(images), self.batch_size)
        ]
        captions: list[Optional[IconCaption]] = []
        for batch in batches:
            logits = np.asarray(
                self._session.run(
                    [self._session_output_name],
                    {self._session_input_name: batch},
                )[0],
                dtype=np.float32,
            )
            probabilities = self._softmax(logits)
            for row in probabilities:
                order = np.argsort(-row)[:5]
                candidates = tuple(
                    (
                        normalize_classifier_label(self._labels[int(index)])
                        or "unknown",
                        float(row[int(index)]),
                    )
                    for index in order
                )
                label, confidence = candidates[0]
                if confidence < self.confidence_threshold:
                    captions.append(
                        IconCaption(
                            raw_caption="unknown",
                            normalized_label=None,
                            confidence=confidence,
                            confidence_provenance=QUASIVISION_CONFIDENCE_PROVENANCE,
                            candidates=candidates,
                            backend="quasivision",
                        )
                    )
                else:
                    captions.append(
                        IconCaption(
                            raw_caption=label,
                            normalized_label=label,
                            confidence=confidence,
                            confidence_provenance=QUASIVISION_CONFIDENCE_PROVENANCE,
                            candidates=candidates,
                            backend="quasivision",
                        )
                    )
        return captions

    @staticmethod
    def _quasivision_preprocess(image: Image.Image) -> np.ndarray:
        image = image.convert("RGBA")
        width, height = image.size
        max_dim = max(width, height, 1)
        scale = QUASIVISION_IMAGE_SIZE / max_dim
        new_size = (
            max(1, int(round(width * scale))),
            max(1, int(round(height * scale))),
        )
        resized = image.resize(new_size, Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", (QUASIVISION_IMAGE_SIZE,) * 2, (255, 255, 255))
        canvas.paste(
            resized,
            (
                (QUASIVISION_IMAGE_SIZE - new_size[0]) // 2,
                (QUASIVISION_IMAGE_SIZE - new_size[1]) // 2,
            ),
            resized,
        )
        pixels = np.asarray(canvas, dtype=np.float32)
        sample = 8
        corners = np.concatenate(
            (
                pixels[:sample, :sample],
                pixels[:sample, -sample:],
                pixels[-sample:, :sample],
                pixels[-sample:, -sample:],
            ),
            axis=0,
        )
        background = corners.reshape(-1, 3).mean(axis=0)
        distance = np.abs(pixels - background).sum(axis=2)
        return (1.0 - np.minimum(distance / (255.0 * 3.0), 1.0)).astype(np.float32)[
            None, ...
        ]

    def _caption_florence2_int8(
        self,
        images: Sequence[Image.Image],
    ) -> list[Optional[IconCaption]]:
        if self._session is None or self._processor is None:
            return [None] * len(images)
        captions: list[Optional[IconCaption]] = []
        rgb_images = [image.convert("RGB") for image in images]
        for start in range(0, len(rgb_images), self.batch_size):
            batch = rgb_images[start : start + self.batch_size]
            inputs = self._processor(
                images=batch,
                text=[GENERATION_PROMPT] * len(batch),
                return_tensors="np",
                do_resize=False,
            )
            input_ids = self._as_numpy(inputs["input_ids"], np.int64)
            pixel_values = self._as_numpy(inputs["pixel_values"], np.float32)
            decoder_ids = np.full(
                (len(batch), 1), self._decoder_start_token_id, dtype=np.int64
            )
            generated: list[np.ndarray] = []
            finished = np.zeros(len(batch), dtype=bool)
            for _ in range(self.max_new_tokens):
                logits = np.asarray(
                    self._session.run(
                        [self._session_output_name],
                        {
                            "input_ids": input_ids,
                            "pixel_values": pixel_values,
                            "decoder_input_ids": decoder_ids,
                        },
                    )[0],
                    dtype=np.float32,
                )
                if logits.ndim == 3:
                    logits = logits[:, -1, :]
                next_ids = np.argmax(logits, axis=-1).astype(np.int64)
                generated.append(next_ids)
                finished |= next_ids == self._eos_token_id
                decoder_ids = np.concatenate(
                    (
                        decoder_ids,
                        np.where(finished, self._eos_token_id, next_ids)[:, None],
                    ),
                    axis=1,
                )
                if bool(finished.all()):
                    break
            generated_ids = (
                np.stack(generated, axis=1)
                if generated
                else np.empty((len(batch), 0), dtype=np.int64)
            )
            generated_texts = self._processor.batch_decode(
                generated_ids,
                skip_special_tokens=True,
            )
            if len(generated_texts) != len(batch):
                raise RuntimeError(
                    "Florence-2 INT8 returned "
                    f"{len(generated_texts)} captions for {len(batch)} icon crops"
                )
            captions.extend(
                IconCaption(
                    raw_caption=re.sub(r"\s+", " ", str(raw)).strip(),
                    normalized_label=normalize_caption(str(raw)),
                    confidence_provenance=FLORENCE2_INT8_CONFIDENCE_PROVENANCE,
                    backend="florence2_int8",
                )
                for raw in generated_texts
            )
        return captions

    @staticmethod
    def _as_numpy(value: Any, dtype: Any) -> np.ndarray:
        if hasattr(value, "detach"):
            value = value.detach().cpu().numpy()
        return np.asarray(value, dtype=dtype)

    @staticmethod
    def _softmax(logits: np.ndarray) -> np.ndarray:
        shifted = logits - np.max(logits, axis=-1, keepdims=True)
        values = np.exp(shifted)
        return values / np.sum(values, axis=-1, keepdims=True)

    @staticmethod
    def _create_onnx_session(path: Path) -> Any:
        if ort is None:
            raise RuntimeError("onnxruntime is unavailable")
        options = ort.SessionOptions()
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        return ort.InferenceSession(
            str(path),
            sess_options=options,
            providers=["CPUExecutionProvider"],
        )

    @staticmethod
    def _require_local_file(value: str, label: str) -> Path:
        path = Path(value).expanduser()
        if not path.is_file():
            raise FileNotFoundError(
                f"{label} must be a local file: {path}. "
                "Runtime model download is disabled; install a complete "
                "cua-pywin release or update the developer model bundle."
            )
        return path.resolve()
