"""YOLO/OCR locations are sufficient; icon semantics belong to the caller's LLM."""

from types import SimpleNamespace

from PIL import Image

from computer_server.handlers import som_bridge


def test_conversion_keeps_unlabeled_actionable_icons_without_decoding_image(monkeypatch):
    def unexpected_image_decode(*args, **kwargs):
        raise AssertionError("Node conversion must not decode or crop the screenshot")

    monkeypatch.setattr(Image, "open", unexpected_image_decode)
    result = SimpleNamespace(
        elements=[
            SimpleNamespace(
                type="icon",
                bbox=SimpleNamespace(coordinates=[0.1, 0.2, 0.3, 0.4]),
                confidence=0.92,
            ),
            SimpleNamespace(
                type="text",
                bbox=SimpleNamespace(coordinates=[0.5, 0.6, 0.9, 0.7]),
                confidence=0.87,
                content="搜索",
            ),
        ],
        metadata=SimpleNamespace(stage_timings={"yolo_latency": 0.1, "ocr_latency": 0.2}),
        annotated_image_base64=None,
    )

    converted = som_bridge._convert_parse_result(result, 1000, 500, 10000)
    icon, text = converted.elements
    assert icon.element_index == 10000
    assert icon.bbox == (100, 100, 300, 200)
    assert (icon.center_x, icon.center_y) == (200, 150)
    assert icon.name is None
    assert icon.actions == ["click"]
    assert icon.metadata == {"vision_confidence": 0.92}
    assert text.element_index == 10001
    assert text.name == "搜索"
    assert text.bbox == (500, 300, 900, 350)
    assert text.metadata["ocr_confidence"] == 0.87
    assert converted.metadata["num_icons"] == 1
    assert converted.metadata["num_text"] == 1
    assert set(converted.metadata["stage_timings"]) == {
        "yolo_latency", "ocr_latency", "bridge_convert_latency",
    }


def test_malformed_node_does_not_break_remaining_indexes():
    result = SimpleNamespace(
        elements=[
            SimpleNamespace(type="icon", bbox=None),
            SimpleNamespace(
                type="icon",
                bbox=SimpleNamespace(coordinates=[0, 0, 1, 1]),
                confidence=0.8,
            ),
        ],
    )
    converted = som_bridge._convert_parse_result(result, 100, 100, 10000)
    assert len(converted.elements) == 1
    assert converted.elements[0].element_index == 10000
    assert converted.elements[0].bbox == (0, 0, 100, 100)
