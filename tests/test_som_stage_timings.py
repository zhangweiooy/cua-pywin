from __future__ import annotations

from io import BytesIO

from PIL import Image

from som.detect import OmniParser


def test_omniparser_reports_stage_timings():
    parser = OmniParser(ocr_languages=["en"])
    parser.detector.detect_icons = lambda **kwargs: [
        {"bbox": [0.1, 0.1, 0.3, 0.3], "confidence": 0.9, "scale": 1}
    ]
    parser.ocr.detect_text = lambda **kwargs: [
        {"bbox": [0.5, 0.5, 0.8, 0.7], "content": "OK", "confidence": 0.8}
    ]
    parser.visualizer.draw_boxes = lambda image, detections, draw_config: image

    image = Image.new("RGB", (20, 20), "white")
    buf = BytesIO()
    image.save(buf, format="PNG")

    result = parser.parse(buf.getvalue(), use_ocr=True)
    timings = result.metadata.stage_timings

    assert result.metadata.latency >= 0
    for key in (
        "image_decode_latency",
        "yolo_latency",
        "ocr_latency",
        "merge_latency",
        "visualization_latency",
        "annotation_encode_latency",
        "omniparser_latency",
        "total_latency",
    ):
        assert key in timings
        assert timings[key] >= 0


def test_detection_failure_preserves_ocr_and_reports_diagnostic():
    parser = OmniParser(ocr_languages=["en"])
    ocr_calls = 0

    def fail_detection(**_kwargs):
        raise RuntimeError("injected ONNX failure")

    def detect_text(**_kwargs):
        nonlocal ocr_calls
        ocr_calls += 1
        return [
            {
                "bbox": [0.1, 0.1, 0.4, 0.3],
                "content": "Search",
                "confidence": 0.9,
            }
        ]

    parser.detector.detect_icons = fail_detection
    parser.ocr.detect_text = detect_text
    parser.visualizer.draw_boxes = lambda image, detections, draw_config: image
    image = Image.new("RGB", (20, 20), "white")
    buf = BytesIO()
    image.save(buf, format="PNG")

    result = parser.parse(buf.getvalue(), use_ocr=True)

    assert ocr_calls == 1
    assert result.metadata.num_icons == 0
    assert result.metadata.num_text == 1
    assert result.elements[0].content == "Search"
    assert result.metadata.icon_detection_error == "injected ONNX failure"
