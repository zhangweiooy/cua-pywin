from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import time
import threading

import numpy as np
import pytest
from PIL import Image
from tokenizers import Tokenizer
from transformers import BartTokenizerFast, CLIPImageProcessor

from computer_server.services.community_florence2_service import _CaptionProcessor
from computer_server.services.icon_caption_service import (
    BUNDLED_FLORENCE2_COMMUNITY_ONNX_DIR,
    IconCaptionService,
)
from computer_server.handlers import som_bridge


def test_lightweight_processor_matches_official_florence_processor() -> None:
    bundle = Path(BUNDLED_FLORENCE2_COMMUNITY_ONNX_DIR)
    spec = importlib.util.spec_from_file_location(
        "official_florence2_processing_for_test",
        bundle / "processing_florence2.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    official = module.Florence2Processor(
        CLIPImageProcessor.from_pretrained(str(bundle), local_files_only=True),
        BartTokenizerFast.from_pretrained(str(bundle), local_files_only=True),
    )
    image_config = json.loads(
        (bundle / "preprocessor_config.json").read_text(encoding="utf-8")
    )
    lightweight = _CaptionProcessor(
        image_config,
        Tokenizer.from_file(str(bundle / "tokenizer.json")),
    )
    images = [
        Image.new("RGB", (64, 64), "white"),
        Image.new("RGB", (64, 64), (30, 34, 42)),
        Image.new("RGB", (64, 64), (10, 20, 30)),
        Image.fromarray(
            np.random.default_rng(42).integers(0, 256, (64, 64, 3), dtype=np.uint8)
        ),
        Image.fromarray(
            np.tile(np.arange(256, dtype=np.uint8), (48, 1)).reshape(64, 64, 3)
        ),
    ]

    official_inputs = official(
        images=images,
        text=["<CAPTION>"] * len(images),
        return_tensors="np",
        do_resize=False,
    )
    lightweight_inputs = lightweight(
        images=images,
        text=["<CAPTION>"] * len(images),
        return_tensors="np",
        do_resize=False,
    )

    np.testing.assert_array_equal(
        official_inputs["input_ids"], lightweight_inputs["input_ids"]
    )
    np.testing.assert_allclose(
        official_inputs["pixel_values"],
        lightweight_inputs["pixel_values"],
        rtol=0.0,
        atol=1e-6,
    )
    for text in (
        "search",
        "搜索按钮",
        "a gear , settings .",
        "don't click",
        "<loc_1> close",
    ):
        generated_ids = [lightweight.tokenizer.encode(text).ids]
        for skip_special_tokens in (True, False):
            assert official.batch_decode(
                generated_ids, skip_special_tokens=skip_special_tokens
            ) == lightweight.batch_decode(
                generated_ids, skip_special_tokens=skip_special_tokens
            )


def test_disabled_semantics_reports_disabled():
    service = IconCaptionService(enabled=False)
    assert service.caption_icons([Image.new("RGB", (64, 64))]) == [None]
    assert service.status["state"] == "disabled"


def test_inference_does_not_hold_status_lock_or_queue(monkeypatch):
    service = IconCaptionService(enabled=True)
    service._state = "ready"
    entered, release = threading.Event(), threading.Event()

    def slow(_images):
        entered.set()
        assert release.wait(2)
        return [None]

    monkeypatch.setattr(service, "_caption_icons_ready", slow)
    images = [Image.new("RGB", (64, 64))]
    thread = threading.Thread(target=service.caption_icons, args=(images,))
    thread.start()
    try:
        assert entered.wait(1)
        assert service.status["state"] == "ready"
        with pytest.raises(RuntimeError, match="already in progress"):
            service.caption_icons(images)
    finally:
        release.set()
        thread.join(2)


def test_success_clears_previous_inference_error(monkeypatch):
    service = IconCaptionService(enabled=True)
    service._state = "ready"
    service._inference_failure = "previous failure"
    monkeypatch.setattr(
        service, "_caption_icons_ready", lambda images: [None] * len(images)
    )
    service.caption_icons([Image.new("RGB", (64, 64))])
    assert service.status["inference_failure"] is None


def test_captioning_does_not_wait_for_loading_backend() -> None:
    service = IconCaptionService(enabled=True)
    service._state = "loading"

    started = time.perf_counter()
    captions = service.caption_icons([Image.new("RGB", (64, 64), "white")])

    assert captions == [None]
    assert time.perf_counter() - started < 0.1


def test_semantic_warmup_thread_does_not_block_parser_ready(monkeypatch) -> None:
    entered = threading.Event()
    release = threading.Event()

    def slow_semantic_warmup() -> bool:
        entered.set()
        release.wait(timeout=1.0)
        return True

    monkeypatch.setattr(
        "computer_server.services.icon_caption_service.warm_up",
        slow_semantic_warmup,
    )
    started = time.perf_counter()
    som_bridge._start_icon_semantic_warmup("test-warmup")

    assert time.perf_counter() - started < 0.1
    assert entered.wait(timeout=0.5)
    release.set()
