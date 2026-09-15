"""Tests for Florence-2 caption metadata merged into SOM vision nodes."""

from __future__ import annotations

from io import BytesIO
from types import SimpleNamespace

from PIL import Image

from computer_server.handlers import som_bridge
from computer_server.services.icon_caption_service import IconCaption


def _bbox(x1, y1, x2, y2):
    return SimpleNamespace(coordinates=[x1, y1, x2, y2])


def test_convert_parse_result_applies_florence2_icon_caption(monkeypatch):
    image = Image.new("RGB", (20, 20), "white")
    buf = BytesIO()
    image.save(buf, format="PNG")

    icon = SimpleNamespace(type="icon", bbox=_bbox(0.1, 0.1, 0.4, 0.4), confidence=0.9)
    text = SimpleNamespace(type="text", bbox=_bbox(0.5, 0.5, 0.9, 0.7), confidence=0.8, content="搜索")
    result = SimpleNamespace(
        elements=[icon, text],
        annotated_image_base64=None,
        metadata=SimpleNamespace(
            latency=0.1,
            device="cpu",
            stage_timings={"yolo_latency": 0.01, "ocr_latency": 0.02},
        ),
    )
    monkeypatch.setattr(
        som_bridge,
        "_label_icon_elements",
        lambda *args, **kwargs: (
            {
                0: IconCaption(
                    raw_caption="a send button",
                    normalized_label="send button",
                )
            },
            "ready",
        ),
    )

    converted = som_bridge._convert_parse_result(
        result,
        buf.getvalue(),
        screen_width=20,
        screen_height=20,
        vision_index_start=10000,
    )

    assert converted.elements[0].name == "send button"
    assert converted.elements[0].metadata["vision_confidence"] == 0.9
    assert (
        converted.elements[0].metadata["icon_label_source"]
        == "omniparser_v2_florence2_int8_community"
    )
    assert converted.elements[0].metadata["raw_caption"] == "a send button"
    assert converted.elements[0].metadata["normalized_label"] == "send button"
    assert converted.elements[0].metadata["icon_label_confidence"] is None
    assert converted.elements[1].name == "搜索"
    assert converted.elements[1].metadata["vision_confidence"] == 0.8
    assert converted.elements[1].metadata["ocr_confidence"] == 0.8
    assert converted.metadata["icon_label_count"] == 1
    assert converted.metadata["icon_caption_count"] == 1
    assert converted.metadata["icon_semantic_backend"] == "florence2_int8_community"
    assert converted.metadata["icon_semantic_status"] == "ready"
    assert converted.metadata["stage_timings"]["yolo_latency"] == 0.01
    assert converted.metadata["stage_timings"]["ocr_latency"] == 0.02
    assert converted.metadata["stage_timings"]["tinyclip_latency"] == 0.0
    assert converted.metadata["stage_timings"]["florence2_latency"] >= 0
    assert converted.metadata["stage_timings"]["icon_semantic_latency"] >= 0
    assert converted.metadata["stage_timings"]["bridge_convert_latency"] >= 0


def test_convert_parse_result_keeps_icons_when_captioning_fails(monkeypatch):
    image = Image.new("RGB", (20, 20), "white")
    buf = BytesIO()
    image.save(buf, format="PNG")
    icon = SimpleNamespace(
        type="icon",
        bbox=_bbox(0.1, 0.1, 0.4, 0.4),
        confidence=0.9,
    )
    result = SimpleNamespace(
        elements=[icon],
        annotated_image_base64=None,
        metadata=SimpleNamespace(
            latency=0.1,
            device="cpu",
            stage_timings={"yolo_latency": 0.01},
        ),
    )
    monkeypatch.setattr(
        som_bridge,
        "_label_icon_elements",
        lambda *args, **kwargs: ({}, "inference_failed"),
    )

    converted = som_bridge._convert_parse_result(
        result,
        buf.getvalue(),
        screen_width=20,
        screen_height=20,
        vision_index_start=10000,
    )

    assert len(converted.elements) == 1
    assert converted.elements[0].name is None
    assert converted.elements[0].actions == ["click"]
    assert converted.metadata["icon_caption_count"] == 0
    assert converted.metadata["icon_semantic_status"] == "inference_failed"


def test_force_som_requests_florence_recovery_during_conversion(monkeypatch):
    image = Image.new("RGB", (20, 20), "white")
    buf = BytesIO()
    image.save(buf, format="PNG")
    icon = SimpleNamespace(
        type="icon",
        bbox=_bbox(0.1, 0.1, 0.4, 0.4),
        confidence=0.9,
    )
    result = SimpleNamespace(
        elements=[icon],
        annotated_image_base64=None,
        metadata=SimpleNamespace(stage_timings={}),
    )
    retry_flags = []

    def fake_label(*args, **kwargs):
        retry_flags.append(kwargs["retry_failed_warmup"])
        return {}, "loading"

    monkeypatch.setattr(som_bridge, "_label_icon_elements", fake_label)

    converted = som_bridge._convert_parse_result(
        result,
        buf.getvalue(),
        screen_width=20,
        screen_height=20,
        vision_index_start=10000,
        retry_failed_warmup=True,
    )

    assert retry_flags == [True]
    assert converted.elements[0].name is None
    assert converted.metadata["icon_semantic_status"] == "loading"
