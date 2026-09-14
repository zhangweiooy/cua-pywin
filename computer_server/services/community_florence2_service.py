"""Runtime for the ONNX Community split Florence-2 graphs.

The source graphs are the OmniParser-specialized Florence-2 conversion.  The
vision and token-embedding graphs stay FP32; the encoder and cached decoder
graphs may point at the official ONNX Runtime INT8 pipeline output.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence

import numpy as np
from PIL import Image

try:
    import onnxruntime as ort
except Exception:  # pragma: no cover - reported by the caller's load state
    ort = None  # type: ignore[assignment]


class CommunityFlorence2Runner:
    """Run split ONNX graphs with encoder-decoder KV-cache reuse."""

    def __init__(
        self,
        source_dir: str | Path,
        pipeline_dir: str | Path,
        *,
        quantized: bool = True,
        batch_size: int = 64,
        max_new_tokens: int = 20,
    ) -> None:
        self.source_dir = Path(source_dir).expanduser().resolve()
        self.pipeline_dir = Path(pipeline_dir).expanduser().resolve()
        self.quantized = quantized
        self.batch_size = max(1, min(256, int(batch_size)))
        self.max_new_tokens = max(1, min(128, int(max_new_tokens)))
        self._sessions: dict[str, Any] = {}
        self._processor: Any = None
        self.decoder_start_token_id = 2
        self.eos_token_id = 2
        self._generation_config: dict[str, Any] = {}

    @staticmethod
    def _session(path: Path) -> Any:
        if ort is None:
            raise RuntimeError("onnxruntime is unavailable")
        if not path.is_file():
            raise FileNotFoundError(path)
        options = ort.SessionOptions()
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        return ort.InferenceSession(
            str(path),
            sess_options=options,
            providers=["CPUExecutionProvider"],
        )

    def _load_processor(self) -> Any:
        with (self.source_dir / "preprocessor_config.json").open(
            "r", encoding="utf-8"
        ) as handle:
            image_config = json.load(handle)
        from tokenizers import Tokenizer

        tokenizer = Tokenizer.from_file(str(self.source_dir / "tokenizer.json"))
        tokenizer_config = json.loads(
            (self.source_dir / "tokenizer_config.json").read_text(encoding="utf-8")
        )
        return _CaptionProcessor(
            image_config,
            tokenizer,
            clean_up_tokenization_spaces=tokenizer_config.get(
                "clean_up_tokenization_spaces", True
            ),
        )

    def load(self) -> None:
        if self._processor is not None:
            return
        with (self.source_dir / "generation_config.json").open(
            "r", encoding="utf-8"
        ) as handle:
            generation_config = json.load(handle)
        self._generation_config = generation_config
        self.decoder_start_token_id = int(
            generation_config.get("decoder_start_token_id", 2)
        )
        self.eos_token_id = int(generation_config.get("eos_token_id", 2))

        source_onnx = self.source_dir / "onnx"
        transformer_graphs = (
            self.pipeline_dir / "int8" if self.quantized else source_onnx
        )
        graph_suffix = ".int8.onnx" if self.quantized else ".onnx"
        processor = self._load_processor()
        sessions = {
            "vision": self._session(source_onnx / "vision_encoder.onnx"),
            "embed": self._session(source_onnx / "embed_tokens.onnx"),
            "encoder": self._session(
                transformer_graphs / f"encoder_model{graph_suffix}"
            ),
            "decoder": self._session(
                transformer_graphs / f"decoder_model{graph_suffix}"
            ),
            "decoder_with_past": self._session(
                transformer_graphs / f"decoder_with_past_model{graph_suffix}"
            ),
        }
        with_past_inputs = {
            item.name for item in sessions["decoder_with_past"].get_inputs()
        }
        if "inputs_embeds" not in with_past_inputs:
            raise ValueError("community decoder_with_past graph lacks inputs_embeds")
        self._sessions = sessions
        self._processor = processor

    @property
    def providers(self) -> dict[str, list[str]]:
        return {
            name: session.get_providers() for name, session in self._sessions.items()
        }

    def caption_images(self, images: Sequence[Image.Image]) -> list[str]:
        if not images:
            return []
        self.load()
        captions: list[str] = []
        rgb_images = [image.convert("RGB") for image in images]
        for start in range(0, len(rgb_images), self.batch_size):
            captions.extend(
                self._caption_batch(rgb_images[start : start + self.batch_size])
            )
        return captions

    def _caption_batch(self, images: Sequence[Image.Image]) -> list[str]:
        processor = self._processor
        inputs = processor(
            images=list(images),
            text=["<CAPTION>"] * len(images),
            return_tensors="np",
            do_resize=False,
        )
        input_ids = np.asarray(inputs["input_ids"], dtype=np.int64)
        pixel_values = np.asarray(inputs["pixel_values"], dtype=np.float32)

        image_features = self._sessions["vision"].run(
            ["image_features"], {"pixel_values": pixel_values}
        )[0]
        text_embeddings = self._sessions["embed"].run(
            ["inputs_embeds"], {"input_ids": input_ids}
        )[0]
        encoder_inputs = np.concatenate((image_features, text_embeddings), axis=1)
        attention_mask = np.ones(encoder_inputs.shape[:2], dtype=np.int64)
        encoder_hidden_states = self._sessions["encoder"].run(
            ["last_hidden_state"],
            {
                "inputs_embeds": encoder_inputs,
                "attention_mask": attention_mask,
            },
        )[0]

        decoder_ids = np.full(
            (len(images), 1), self.decoder_start_token_id, dtype=np.int64
        )
        decoder_embeddings = self._sessions["embed"].run(
            ["inputs_embeds"], {"input_ids": decoder_ids}
        )[0]
        decoder = self._sessions["decoder"]
        initial_outputs = decoder.run(
            None,
            {
                "encoder_hidden_states": encoder_hidden_states,
                "inputs_embeds": decoder_embeddings,
            },
        )
        logits = np.asarray(initial_outputs[0], dtype=np.float32)[:, -1, :]
        past = self._initial_past(decoder, initial_outputs)

        generated: list[np.ndarray] = []
        finished = np.zeros(len(images), dtype=bool)
        for step in range(self.max_new_tokens):
            if not np.isfinite(logits).all():
                raise ValueError("Florence decoder produced non-finite logits")
            self._process_logits(logits, generated, step)
            next_ids = np.argmax(logits, axis=-1).astype(np.int64)
            next_ids = np.where(finished, self.eos_token_id, next_ids)
            generated.append(next_ids)
            finished |= next_ids == self.eos_token_id
            if bool(finished.all()) or step + 1 == self.max_new_tokens:
                break
            decoder_ids = np.where(finished, self.eos_token_id, next_ids)[:, None]
            decoder_embeddings = self._sessions["embed"].run(
                ["inputs_embeds"], {"input_ids": decoder_ids}
            )[0]
            step_outputs = self._sessions["decoder_with_past"].run(
                None,
                {
                    "inputs_embeds": decoder_embeddings,
                    **past,
                },
            )
            logits = np.asarray(step_outputs[0], dtype=np.float32)[:, -1, :]
            past = self._step_past(
                past, step_outputs, self._sessions["decoder_with_past"]
            )

        generated_ids = (
            np.stack(generated, axis=1)
            if generated
            else np.empty((len(images), 0), dtype=np.int64)
        )
        decoded = processor.batch_decode(generated_ids, skip_special_tokens=True)
        if len(decoded) != len(images):
            raise RuntimeError(
                "community Florence-2 returned "
                f"{len(decoded)} captions for {len(images)} icon crops"
            )
        return [str(text) for text in decoded]

    @staticmethod
    def _initial_past(session: Any, outputs: Sequence[Any]) -> dict[str, Any]:
        past: dict[str, Any] = {}
        for meta, value in zip(session.get_outputs()[1:], outputs[1:]):
            if meta.name.startswith("present."):
                past["past_key_values." + meta.name[len("present.") :]] = value
        return past

    @staticmethod
    def _step_past(
        previous: dict[str, Any],
        outputs: Sequence[Any],
        session: Any,
    ) -> dict[str, Any]:
        # decoder_with_past returns decoder self-attention present tensors only;
        # encoder cross-attention KV tensors are reused from the first graph.
        past = {name: value for name, value in previous.items() if ".encoder." in name}
        past.update(CommunityFlorence2Runner._initial_past(session, outputs))
        return past

    def _process_logits(
        self, logits: np.ndarray, generated: list[np.ndarray], step: int
    ) -> None:
        """Apply the bundled greedy generation settings, including start/end tokens."""
        forced = (
            self._generation_config.get("forced_bos_token_id") if step == 0 else None
        )
        if step + 1 == self.max_new_tokens:
            forced = self._generation_config.get("forced_eos_token_id")
        if forced is not None:
            logits[:] = -np.inf
            logits[:, int(forced)] = 0
            return
        n = int(self._generation_config.get("no_repeat_ngram_size", 0))
        if n <= 0:
            return
        for row in range(logits.shape[0]):
            history = [self.decoder_start_token_id] + [
                int(ids[row]) for ids in generated
            ]
            prefix = history[-(n - 1) :] if n > 1 else []
            for index in range(len(history) - n + 1):
                if history[index : index + n - 1] == prefix:
                    logits[row, history[index + n - 1]] = -np.inf


class _CaptionProcessor:
    """Caption-only preprocessing without importing Florence's PyTorch code."""

    _TASK_PROMPTS = {
        "<OCR>": "What is the text in the image?",
        "<OCR_WITH_REGION>": "What is the text in the image, with regions?",
        "<CAPTION>": "What does the image describe?",
        "<DETAILED_CAPTION>": "Describe in detail what is shown in the image.",
        "<MORE_DETAILED_CAPTION>": "Describe with a paragraph what is shown in the image.",
        "<OD>": "Locate the objects with category name in the image.",
        "<DENSE_REGION_CAPTION>": "Locate the objects in the image, with their descriptions.",
        "<REGION_PROPOSAL>": "Locate the region proposals in the image.",
    }

    def __init__(
        self,
        image_config: dict[str, Any],
        tokenizer: Any,
        *,
        clean_up_tokenization_spaces: bool = True,
    ) -> None:
        self.image_config = image_config
        self.tokenizer = tokenizer
        self.clean_up_tokenization_spaces = clean_up_tokenization_spaces

    def __call__(
        self,
        *,
        images: Sequence[Image.Image],
        text: Sequence[str],
        return_tensors: str,
        do_resize: bool,
    ) -> dict[str, np.ndarray]:
        prompts = [self._construct_prompt(value) for value in text]
        pixels = np.stack([self._image_to_numpy(image, do_resize) for image in images])
        tokenized = [self.tokenizer.encode(prompt).ids for prompt in prompts]
        if len({len(ids) for ids in tokenized}) > 1:
            raise ValueError("caption prompts must have equal token lengths")
        return {
            "input_ids": np.asarray(tokenized, dtype=np.int64),
            "pixel_values": pixels.astype(np.float32, copy=False),
        }

    def batch_decode(self, token_ids: Any, *, skip_special_tokens: bool) -> list[str]:
        decoded = [
            self.tokenizer.decode(
                np.asarray(ids, dtype=np.int64).tolist(),
                skip_special_tokens=skip_special_tokens,
            )
            for ids in token_ids
        ]
        if self.clean_up_tokenization_spaces:
            # Same replacements as Transformers' PreTrainedTokenizerBase.
            for index, text in enumerate(decoded):
                for before, after in (
                    (" .", "."),
                    (" ?", "?"),
                    (" !", "!"),
                    (" ,", ","),
                    (" ' ", "'"),
                    (" n't", "n't"),
                    (" 'm", "'m"),
                    (" 's", "'s"),
                    (" 've", "'ve"),
                    (" 're", "'re"),
                ):
                    text = text.replace(before, after)
                decoded[index] = text
        return decoded

    def _image_to_numpy(self, image: Image.Image, do_resize: bool) -> np.ndarray:
        rgb = image.convert("RGB")
        if do_resize:
            size = self.image_config.get("size", {})
            rgb = rgb.resize(
                (
                    int(size.get("width", 768)),
                    int(size.get("height", 768)),
                ),
                Image.Resampling.BICUBIC,
            )
        pixels = np.asarray(rgb, dtype=np.float32) / 255.0
        mean = np.asarray(self.image_config["image_mean"], dtype=np.float32)
        std = np.asarray(self.image_config["image_std"], dtype=np.float32)
        return np.transpose((pixels - mean) / std, (2, 0, 1))

    @classmethod
    def _construct_prompt(cls, text: str) -> str:
        value = str(text)
        for task_token, prompt in cls._TASK_PROMPTS.items():
            if task_token in value:
                if value != task_token:
                    raise ValueError(
                        f"Task token {task_token} must be the only caption prompt"
                    )
                return prompt
        return value
