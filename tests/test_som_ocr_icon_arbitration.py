"""Deterministic OCR/icon collision arbitration regressions."""

from __future__ import annotations

from io import BytesIO
from types import SimpleNamespace

from PIL import Image

from computer_server.handlers import som_bridge
from som.detect import OmniParser


def _parser(icon_detections, text_detections):
    parser = object.__new__(OmniParser)
    parser.detector = SimpleNamespace(
        detect_icons=lambda **_kwargs: list(icon_detections),
        device="cpu",
    )
    parser.ocr = SimpleNamespace(detect_text=lambda **_kwargs: list(text_detections))
    parser.visualizer = SimpleNamespace(
        draw_boxes=lambda image, detections, draw_config: image
    )
    parser._last_stage_timings = {}
    parser._last_collision_diagnostics = {}
    return parser


def _image_and_bytes():
    image = Image.new("RGB", (100, 100), "white")
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return image, buffer.getvalue()


def _file_icon(confidence=0.72):
    return {"bbox": [0.2, 0.2, 0.5, 0.5], "confidence": confidence, "scale": 1}


def _ocr(content="口", confidence=0.79):
    return {
        "bbox": [0.25, 0.25, 0.45, 0.45],
        "content": content,
        "confidence": confidence,
    }


def test_process_image_preserves_icon_for_ambiguous_short_ocr_collision():
    parser = _parser([_file_icon()], [_ocr()])
    image, _ = _image_and_bytes()

    _, elements = parser.process_image(image, use_ocr=True)

    assert {element.type for element in elements} == {"icon", "text"}
    assert any(element.type == "icon" for element in elements)
    assert parser._last_collision_diagnostics == {
        "raw_icon_count": 1,
        "raw_text_count": 1,
        "ocr_icon_collision_count": 1,
        "icons_suppressed_by_ocr": 0,
        "ambiguous_collisions_preserved": 1,
    }


def test_clear_search_text_suppresses_overlapping_duplicate_icon():
    parser = _parser([_file_icon(0.9)], [_ocr("搜索", 0.8)])
    image, _ = _image_and_bytes()

    _, elements = parser.process_image(image, use_ocr=True)

    assert len(elements) == 1
    assert elements[0].type == "text"
    assert elements[0].content == "搜索"
    assert parser._last_collision_diagnostics["icons_suppressed_by_ocr"] == 1


def test_protected_icon_survives_cross_type_nms_when_ocr_score_is_higher():
    parser = _parser([_file_icon(0.7)], [_ocr(confidence=0.8)])
    image, _ = _image_and_bytes()

    _, elements = parser.process_image(image, iou_threshold=0.1, use_ocr=True)

    assert len(elements) == 2
    assert any(element.type == "icon" for element in elements)
    assert any(element.type == "text" for element in elements)


def test_bridge_keeps_icon_and_ocr_locations_for_llm_interpretation():
    parser = _parser([_file_icon()], [_ocr("中", 0.9)])
    _, screenshot_bytes = _image_and_bytes()
    result = parser.parse(screenshot_bytes, use_ocr=True)

    converted = som_bridge._convert_parse_result(
        result, 100, 100, vision_index_start=10000
    )

    assert {node.control_type for node in converted.elements} == {"icon", "text"}
    icon = next(node for node in converted.elements if node.control_type == "icon")
    text = next(node for node in converted.elements if node.control_type == "text")
    assert icon.name is None
    assert icon.actions == ["click"]
    assert icon.bbox == (20, 20, 50, 50)
    assert text.name == "中"
    assert text.metadata["ocr_confidence"] == 0.9
    assert text.bbox == (25, 25, 45, 45)
    assert icon.element_index != text.element_index
    assert all(
        node.metadata["semantic_uncertainty"] == "ocr_icon_collision"
        for node in converted.elements
    )
    assert converted.metadata["num_icons"] == 1
    assert converted.metadata["num_text"] == 1
