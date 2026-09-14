"""Experimental beam search used only by offline quality benchmarks."""

from __future__ import annotations
from typing import Sequence
import numpy as np
from PIL import Image
from computer_server.services.community_florence2_service import (
    CommunityFlorence2Runner,
)


class BenchmarkFlorence2Runner(CommunityFlorence2Runner):
    def caption_images_topk(
        self,
        images: Sequence[Image.Image],
        *,
        beam_width: int = 3,
    ) -> list[tuple[str, ...]]:
        """Return benchmark-only beam candidates without changing production decoding."""
        if not images:
            return []
        self.load()
        beam_width = max(1, min(8, int(beam_width)))
        candidates: list[tuple[str, ...]] = []
        rgb_images = [image.convert("RGB") for image in images]
        for start in range(0, len(rgb_images), self.batch_size):
            candidates.extend(
                self._caption_batch_topk(
                    rgb_images[start : start + self.batch_size],
                    beam_width=beam_width,
                )
            )
        return candidates

    def _caption_batch_topk(
        self,
        images: Sequence[Image.Image],
        *,
        beam_width: int,
    ) -> list[tuple[str, ...]]:
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

        batch_size = len(images)
        decoder_ids = np.full(
            (batch_size, 1), self.decoder_start_token_id, dtype=np.int64
        )
        decoder_embeddings = self._sessions["embed"].run(
            ["inputs_embeds"], {"input_ids": decoder_ids}
        )[0]
        initial_outputs = self._sessions["decoder"].run(
            None,
            {
                "encoder_hidden_states": encoder_hidden_states,
                "inputs_embeds": decoder_embeddings,
            },
        )
        logits = np.asarray(initial_outputs[0], dtype=np.float32)[:, -1, :]
        beam_width = min(beam_width, logits.shape[-1])
        self._process_logits(logits, [], 0)
        log_probabilities = self._log_softmax(logits)
        selected = np.argpartition(log_probabilities, -beam_width, axis=1)[
            :, -beam_width:
        ]
        selected_scores = np.take_along_axis(log_probabilities, selected, axis=1)
        order = np.argsort(-selected_scores, axis=1)
        tokens = np.take_along_axis(selected, order, axis=1).astype(np.int64)
        scores = np.take_along_axis(selected_scores, order, axis=1)
        sequences = tokens[:, :, None]
        finished = tokens == self.eos_token_id
        initial_past = self._initial_past(self._sessions["decoder"], initial_outputs)
        past = {
            name: np.repeat(value, beam_width, axis=0)
            for name, value in initial_past.items()
        }

        for _ in range(max(0, self.max_new_tokens - 1)):
            if bool(finished.all()):
                break
            next_ids = sequences[:, :, -1].reshape(batch_size * beam_width, 1)
            next_embeddings = self._sessions["embed"].run(
                ["inputs_embeds"], {"input_ids": next_ids}
            )[0]
            step_outputs = self._sessions["decoder_with_past"].run(
                None,
                {
                    "inputs_embeds": next_embeddings,
                    **past,
                },
            )
            logits = np.asarray(step_outputs[0], dtype=np.float32)[:, -1, :]
            flat_sequences = sequences.reshape(batch_size * beam_width, -1)
            self._process_logits(
                logits,
                [flat_sequences[:, i] for i in range(flat_sequences.shape[1])],
                flat_sequences.shape[1],
            )
            vocabulary_size = logits.shape[-1]
            log_probabilities = self._log_softmax(logits).reshape(
                batch_size, beam_width, vocabulary_size
            )
            log_probabilities = np.where(
                finished[:, :, None], -np.inf, log_probabilities
            )
            eos_scores = log_probabilities[:, :, self.eos_token_id]
            log_probabilities[:, :, self.eos_token_id] = np.where(
                finished, 0.0, eos_scores
            )
            candidate_scores = scores[:, :, None] + log_probabilities
            selected = np.argpartition(
                candidate_scores.reshape(batch_size, -1),
                -beam_width,
                axis=1,
            )[:, -beam_width:]
            selected_scores = np.take_along_axis(
                candidate_scores.reshape(batch_size, -1), selected, axis=1
            )
            order = np.argsort(-selected_scores, axis=1)
            selected = np.take_along_axis(selected, order, axis=1)
            scores = np.take_along_axis(selected_scores, order, axis=1)
            previous_beams = selected // vocabulary_size
            tokens = (selected % vocabulary_size).astype(np.int64)
            rows = np.arange(batch_size)[:, None]
            flat_previous = (rows * beam_width + previous_beams).reshape(-1)
            sequences = np.concatenate(
                (
                    sequences[rows, previous_beams],
                    tokens[:, :, None],
                ),
                axis=2,
            )
            finished = finished[rows, previous_beams] | (tokens == self.eos_token_id)
            next_past = self._step_past(
                past, step_outputs, self._sessions["decoder_with_past"]
            )
            past = {name: value[flat_previous] for name, value in next_past.items()}

        decoded = processor.batch_decode(
            sequences.reshape(batch_size * beam_width, -1),
            skip_special_tokens=True,
        )
        return [
            tuple(str(item) for item in decoded[start : start + beam_width])
            for start in range(0, len(decoded), beam_width)
        ]

    @staticmethod
    def _log_softmax(logits: np.ndarray) -> np.ndarray:
        shifted = logits - np.max(logits, axis=-1, keepdims=True)
        return shifted - np.log(np.exp(shifted).sum(axis=-1, keepdims=True))
