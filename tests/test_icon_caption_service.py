from __future__ import annotations

from types import SimpleNamespace

import pytest
from PIL import Image

from computer_server.services.icon_caption_service import (
    BUNDLED_FLORENCE2_COMMUNITY_ONNX_DIR,
    FLORENCE2_COMMUNITY_CONFIDENCE_PROVENANCE,
    IconCaptionService,
    normalize_caption,
)


class FakeCommunityRunner:
    providers = ["CPUExecutionProvider"]

    def __init__(self, captions: list[str]) -> None:
        self.captions = captions
        self.calls = 0

    def caption_images(self, images):
        self.calls += 1
        return self.captions


class ImmediateThread:
    def __init__(self, *, target, kwargs=None, name, daemon):
        self._target = target
        self._kwargs = kwargs or {}
        self.name = name
        self.daemon = daemon

    def start(self):
        self._target(**self._kwargs)

    def is_alive(self):
        return False


def test_icon_caption_service_uses_selected_community_backend():
    service = IconCaptionService(enabled=True)
    service._state = "ready"
    service._community_runner = FakeCommunityRunner(
        ["a scissors icon", "more options button", "icon"]
    )

    captions = service.caption_icons(
        [Image.new("RGB", (64, 64), "white") for _ in range(3)]
    )

    assert service._community_runner.calls == 1
    assert [caption.normalized_label for caption in captions] == [
        "scissors icon",
        "more options button",
        None,
    ]
    assert all(
        caption.confidence_provenance
        == FLORENCE2_COMMUNITY_CONFIDENCE_PROVENANCE
        for caption in captions
    )


def test_normalize_caption_is_conservative():
    assert normalize_caption("  A QR code icon. ") == "QR code icon"
    assert normalize_caption("icon") is None
    assert normalize_caption("") is None
    assert normalize_caption("phone with a slash") == "phone with a slash"
    assert normalize_caption("M0,0L0,5 4.5,5z") is None
    assert normalize_caption("<svg><path d='M0,0'/></svg>") is None


def test_default_model_path_uses_selected_bundle(monkeypatch):
    monkeypatch.delenv("CUA_FLORENCE2_COMMUNITY_ONNX_DIR", raising=False)
    service = IconCaptionService()
    assert service.model_name_or_path == str(BUNDLED_FLORENCE2_COMMUNITY_ONNX_DIR)
    assert service.processor_name_or_path == service.model_name_or_path


def test_remote_model_identifier_is_rejected_without_download():
    service = IconCaptionService(
        model_name_or_path="microsoft/OmniParser-v2.0",
        enabled=True,
    )
    assert service.ensure_ready() is False
    assert service.status["state"] == "failed"
    assert "must be a local directory" in service.status["failure"]


def test_failed_semantic_warmup_retries_once(monkeypatch):
    service = IconCaptionService(enabled=True)
    service._state = "failed"
    service._failure = "initial failure"
    attempts = []

    def fail_load():
        attempts.append(True)
        raise RuntimeError("retry failed")

    monkeypatch.setattr(service, "_load_backend", fail_load)
    monkeypatch.setattr(
        "computer_server.services.icon_caption_service.threading",
        SimpleNamespace(Thread=ImmediateThread),
    )

    assert service.start_background_warm_up(retry_failed=True) is True
    assert service.status["state"] == "failed"
    assert service.status["failure"] == "retry failed"
    assert len(attempts) == 1
    assert service.start_background_warm_up(retry_failed=True) is False
    assert len(attempts) == 1


def test_missing_int8_vision_marks_semantics_failed(monkeypatch, tmp_path):
    from computer_server.services.community_florence2_service import (
        CommunityFlorence2Runner,
    )

    (tmp_path / "generation_config.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        CommunityFlorence2Runner,
        "_load_processor",
        lambda _runner: object(),
    )
    service = IconCaptionService(model_name_or_path=str(tmp_path), enabled=True)

    assert service.ensure_ready() is False
    assert service.status["state"] == "failed"
    assert "int8" in service.status["failure"]
    assert "vision_encoder.int8.onnx" in service.status["failure"]


def test_caption_limits_are_bounded(monkeypatch):
    monkeypatch.setenv("CUA_FLORENCE2_BATCH_SIZE", "9999")
    monkeypatch.setenv("CUA_FLORENCE2_MAX_NEW_TOKENS", "9999")
    service = IconCaptionService()
    assert service.batch_size == 256
    assert service.max_new_tokens == 128


def test_caption_service_rejects_mismatched_batch_output():
    service = IconCaptionService(enabled=True)
    service._state = "ready"
    service._community_runner = FakeCommunityRunner(["only one caption"])
    with pytest.raises(RuntimeError, match="1 captions for 2 icon crops"):
        service.caption_icons(
            [Image.new("RGB", (64, 64), "white") for _ in range(2)]
        )
