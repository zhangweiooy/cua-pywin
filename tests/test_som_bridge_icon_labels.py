from __future__ import annotations

from io import BytesIO
from types import SimpleNamespace

from PIL import Image

from computer_server.handlers import som_bridge
from computer_server.services.icon_label_service import IconLabel


def _bbox(x1, y1, x2, y2):
    return SimpleNamespace(coordinates=[x1, y1, x2, y2])


def test_convert_parse_result_applies_tinyclip_icon_label(monkeypatch):
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
        lambda *args, **kwargs: {0: IconLabel("send button", 0.91, 0.33)},
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
    assert converted.elements[0].metadata["icon_label_source"] == "tinyclip"
    assert converted.elements[0].metadata["icon_label_score"] == 0.91
    assert converted.elements[1].name == "搜索"
    assert converted.elements[1].metadata["vision_confidence"] == 0.8
    assert converted.elements[1].metadata["ocr_confidence"] == 0.8
    assert converted.metadata["icon_label_count"] == 1
    assert converted.metadata["stage_timings"]["yolo_latency"] == 0.01
    assert converted.metadata["stage_timings"]["ocr_latency"] == 0.02
    assert converted.metadata["stage_timings"]["tinyclip_latency"] >= 0
    assert converted.metadata["stage_timings"]["bridge_convert_latency"] >= 0
