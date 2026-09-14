from __future__ import annotations

import sys
import types

import pytest
from PIL import Image

from computer_server.services.icon_caption_service import (
    BUNDLED_FLORENCE2_MODEL_DIR,
    GENERATION_CONFIDENCE_PROVENANCE,
    IconCaptionService,
    default_model_name_or_path,
    normalize_caption,
)


def test_icon_caption_service_batches_images_once():
    import torch

    class FakeProcessor:
        def __init__(self):
            self.call_count = 0
            self.batch_size = 0

        def __call__(self, images=None, text=None, return_tensors="pt", **kwargs):
            assert images is not None
            assert text == ["<CAPTION>"] * len(images)
            assert kwargs["do_resize"] is False
            self.call_count += 1
            self.batch_size = len(images)
            return {
                "input_ids": torch.ones(len(images), 2, dtype=torch.long),
                "pixel_values": torch.ones(len(images), 3, 64, 64),
            }

        def batch_decode(self, generated_ids, skip_special_tokens=True):
            assert skip_special_tokens is True
            assert generated_ids.shape[0] == 3
            return ["a scissors icon", "more options button", "icon"]

    class FakeModel:
        def __init__(self):
            self.generate_call_count = 0

        def generate(self, **kwargs):
            self.generate_call_count += 1
            assert kwargs["num_beams"] == 1
            assert kwargs["do_sample"] is False
            return torch.ones(3, 4, dtype=torch.long)

    processor = FakeProcessor()
    model = FakeModel()
    service = IconCaptionService(backend="florence2", enabled=True, batch_size=64)
    service._state = "ready"
    service._processor = processor
    service._model = model
    service._torch = torch
    service._device = torch.device("cpu")
    service._dtype = torch.float32

    images = [Image.new("RGB", (64, 64), "white") for _ in range(3)]
    captions = service.caption_icons(images)

    assert processor.call_count == 1
    assert processor.batch_size == 3
    assert model.generate_call_count == 1
    assert [caption.normalized_label for caption in captions] == [
        "scissors icon",
        "more options button",
        None,
    ]
    assert all(
        caption.confidence_provenance == GENERATION_CONFIDENCE_PROVENANCE
        for caption in captions
    )


def test_icon_caption_service_chunks_only_above_batch_limit():
    import torch

    class FakeProcessor:
        def __init__(self):
            self.batch_sizes = []

        def __call__(self, images=None, **kwargs):
            self.batch_sizes.append(len(images))
            return {
                "input_ids": torch.ones(len(images), 2, dtype=torch.long),
                "pixel_values": torch.ones(len(images), 3, 64, 64),
            }

        def batch_decode(self, generated_ids, **kwargs):
            return ["close button"] * generated_ids.shape[0]

    class FakeModel:
        def generate(self, input_ids, **kwargs):
            return torch.ones(input_ids.shape[0], 3, dtype=torch.long)

    processor = FakeProcessor()
    service = IconCaptionService(backend="florence2", enabled=True, batch_size=2)
    service._state = "ready"
    service._processor = processor
    service._model = FakeModel()
    service._torch = torch
    service._device = torch.device("cpu")
    service._dtype = torch.float32

    captions = service.caption_icons(
        [Image.new("RGB", (64, 64), "white") for _ in range(5)]
    )

    assert processor.batch_sizes == [2, 2, 1]
    assert [caption.label for caption in captions] == ["close button"] * 5


def test_normalize_caption_is_conservative():
    assert normalize_caption("  A QR code icon. ") == "QR code icon"
    assert normalize_caption("icon") is None
    assert normalize_caption("") is None
    assert normalize_caption("phone with a slash") == "phone with a slash"
    assert normalize_caption("M0,0L0,5 4.5,5z") is None
    assert normalize_caption("<svg><path d='M0,0'/></svg>") is None


def test_default_model_path_uses_bundled_model(monkeypatch):
    monkeypatch.delenv("CUA_FLORENCE2_MODEL", raising=False)

    assert default_model_name_or_path() == str(BUNDLED_FLORENCE2_MODEL_DIR)


def test_remote_model_identifier_is_rejected_without_download():
    service = IconCaptionService(
        model_name_or_path="microsoft/OmniParser-v2.0",
        processor_name_or_path="microsoft/Florence-2-base",
        backend="florence2",
    )

    assert service.ensure_ready() is False
    assert service.status["state"] == "failed"
    assert "must be a local directory" in service.status["failure"]


def test_local_model_loading_forces_transformers_offline(monkeypatch, tmp_path):
    calls = {}

    class FakeProcessor:
        @classmethod
        def from_pretrained(cls, path, **kwargs):
            calls["processor"] = (path, kwargs)
            return cls()

    class FakeModel:
        @classmethod
        def from_pretrained(cls, path, **kwargs):
            calls["model"] = (path, kwargs)
            return cls()

        def eval(self):
            return self

        def to(self, _device):
            return self

    monkeypatch.setitem(
        sys.modules,
        "transformers",
        types.SimpleNamespace(
            AutoModelForCausalLM=FakeModel,
            AutoProcessor=FakeProcessor,
        ),
    )
    service = IconCaptionService(
        model_name_or_path=str(tmp_path),
        processor_name_or_path=str(tmp_path),
        enabled=True,
        backend="florence2",
    )

    assert service.ensure_ready() is True
    assert calls["processor"][1]["local_files_only"] is True
    assert calls["model"][1]["local_files_only"] is True
    assert calls["processor"][0] == str(tmp_path.resolve())
    assert calls["model"][0] == str(tmp_path.resolve())


def test_caption_limits_are_bounded(monkeypatch):
    monkeypatch.setenv("CUA_FLORENCE2_BATCH_SIZE", "9999")
    monkeypatch.setenv("CUA_FLORENCE2_MAX_NEW_TOKENS", "9999")

    service = IconCaptionService()

    assert service.batch_size == 256
    assert service.max_new_tokens == 128


def test_caption_service_rejects_mismatched_batch_output():
    import torch

    class FakeProcessor:
        def __call__(self, images=None, **kwargs):
            return {
                "input_ids": torch.ones(len(images), 2, dtype=torch.long),
                "pixel_values": torch.ones(len(images), 3, 64, 64),
            }

        def batch_decode(self, generated_ids, **kwargs):
            return ["only one caption"]

    class FakeModel:
        def generate(self, input_ids, **kwargs):
            return torch.ones(input_ids.shape[0], 3, dtype=torch.long)

    service = IconCaptionService(backend="florence2", enabled=True)
    service._state = "ready"
    service._processor = FakeProcessor()
    service._model = FakeModel()
    service._torch = torch
    service._device = torch.device("cpu")
    service._dtype = torch.float32

    with pytest.raises(RuntimeError, match="1 captions for 2 icon crops"):
        service.caption_icons(
            [Image.new("RGB", (64, 64), "white") for _ in range(2)]
        )
