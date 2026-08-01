from __future__ import annotations

import statistics

import pytest
from PIL import Image, ImageDraw

from computer_server.handlers.element_cache import CachedElement
from computer_server.services import window_state_service
from computer_server.services.wechat_compose_service import (
    detect_wechat_compose_region,
)


def _make_icon(index: int, bbox: tuple[int, int, int, int]) -> CachedElement:
    return CachedElement(
        element_index=index,
        source="vision",
        center_x=(bbox[0] + bbox[2]) // 2,
        center_y=(bbox[1] + bbox[3]) // 2,
        bbox=bbox,
        control_type="icon",
        name=f"toolbar icon {index}",
        actions=["click"],
    )


def _wechat_fixture(
    width: int = 1280,
    height: int = 930,
    *,
    background: int = 250,
    border: int = 225,
    rounded_gaps: bool = False,
    draft_text: bool = False,
    icon_count: int = 5,
    include_send_text: bool = True,
) -> tuple[Image.Image, list[CachedElement], tuple[int, int, int, int]]:
    image = Image.new("RGB", (width, height), (background,) * 3)
    draw = ImageDraw.Draw(image)
    boundary = (
        int(width * 0.35),
        int(height * 0.76),
        int(width * 0.98),
        int(height * 0.975),
    )
    left, top, right, bottom = boundary
    color = (border,) * 3
    gap = max(0, int(min(width, height) * 0.018)) if rounded_gaps else 0
    draw.line((left + gap, top, right - gap, top), fill=color, width=2)
    draw.line((left + gap, bottom, right - gap, bottom), fill=color, width=2)
    draw.line((left, top + gap, left, bottom - gap), fill=color, width=2)
    draw.line((right, top + gap, right, bottom - gap), fill=color, width=2)

    if draft_text:
        text_color = (max(0, background - 70),) * 3
        draw.line(
            (left + width * 0.04, top + height * 0.06, left + width * 0.25, top + height * 0.06),
            fill=text_color,
            width=2,
        )
        draw.line(
            (left + width * 0.04, top + height * 0.09, left + width * 0.18, top + height * 0.09),
            fill=text_color,
            width=2,
        )

    toolbar_y = bottom - int(height * 0.035)
    icon_size = max(20, int(height * 0.04))
    positions = (0.06, 0.20, 0.34, 0.48, 0.76)
    elements: list[CachedElement] = []
    for index, fraction in enumerate(positions[:icon_count]):
        center_x = left + int((right - left) * fraction)
        bbox = (
            center_x - icon_size // 2,
            toolbar_y - icon_size // 2,
            center_x + icon_size // 2,
            toolbar_y + icon_size // 2,
        )
        elements.append(_make_icon(10000 + index, bbox))
    if include_send_text:
        elements.append(
            CachedElement(
                element_index=10020,
                source="vision",
                center_x=right - int(width * 0.045),
                center_y=toolbar_y,
                bbox=(
                    right - int(width * 0.07),
                    toolbar_y - icon_size // 3,
                    right - int(width * 0.02),
                    toolbar_y + icon_size // 3,
                ),
                control_type="text",
                name="发送",
            )
        )
    return image, elements, boundary


@pytest.mark.parametrize(
    ("width", "height"),
    [(1024, 744), (1280, 930), (1600, 1162)],
    ids=["100-percent", "125-percent", "150-percent"],
)
def test_detects_scaled_wechat_compose_boundary(width: int, height: int):
    image, elements, boundary = _wechat_fixture(width, height)

    result = detect_wechat_compose_region(
        image,
        elements,
        target_family="wechat",
    )

    assert result.element is not None
    assert result.element.control_type == "edit"
    assert result.element.name == "message input"
    assert result.element.actions == ["click", "text"]
    assert result.element.source == "vision"
    assert result.element.metadata["layout_detector"] == "wechat_compose_boundary_v1"
    assert result.diagnostics["compose_region_count"] == 1
    assert result.diagnostics["compose_candidate_count"] >= 1

    safe = result.element.bbox
    toolbar_top = min(element.bbox[1] for element in elements if element.actions)
    assert boundary[0] < safe[0] < safe[2] < boundary[2]
    assert boundary[1] < safe[1] < safe[3] < toolbar_top


@pytest.mark.parametrize(
    ("background", "border", "rounded_gaps", "draft_text"),
    [
        (245, 237, False, False),
        (35, 49, False, False),
        (250, 225, True, False),
        (250, 225, False, True),
    ],
    ids=["low-contrast", "dark", "rounded-corners", "draft-text"],
)
def test_detects_visual_variants(
    background: int,
    border: int,
    rounded_gaps: bool,
    draft_text: bool,
):
    image, elements, _ = _wechat_fixture(
        background=background,
        border=border,
        rounded_gaps=rounded_gaps,
        draft_text=draft_text,
    )

    result = detect_wechat_compose_region(
        image,
        elements,
        target_family="wechat",
    )

    assert result.element is not None
    assert result.element.metadata["boundary_contrast"] >= 6


def test_non_wechat_never_generates_compose_node():
    image, elements, _ = _wechat_fixture()

    result = detect_wechat_compose_region(
        image,
        elements,
        target_family="generic",
    )

    assert result.element is None
    assert result.diagnostics["compose_candidate_count"] == 0


def test_missing_boundary_never_generates_compose_node():
    image, elements, _ = _wechat_fixture()
    image = Image.new("RGB", image.size, "white")

    result = detect_wechat_compose_region(
        image,
        elements,
        target_family="wechat",
    )

    assert result.element is None


def test_missing_toolbar_icons_never_generates_compose_node():
    image, elements, _ = _wechat_fixture(icon_count=3)

    result = detect_wechat_compose_region(
        image,
        elements,
        target_family="wechat",
    )

    assert result.element is None


def test_four_toolbar_icons_are_sufficient_without_send_ocr():
    image, elements, _ = _wechat_fixture(
        icon_count=4,
        include_send_text=False,
    )

    result = detect_wechat_compose_region(
        image,
        elements,
        target_family="wechat",
    )

    assert result.element is not None
    assert result.element.metadata["anchor_icon_count"] == 4
    assert result.element.metadata["layout_confidence"] >= 0.80


def test_search_page_style_rectangle_never_generates_compose_node():
    image = Image.new("RGB", (1280, 930), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((300, 520, 1000, 650), outline=(225, 225, 225), width=2)
    _fixture_image, elements, _ = _wechat_fixture()

    result = detect_wechat_compose_region(
        image,
        elements,
        target_family="wechat",
    )

    assert result.element is None


def test_occluded_boundary_never_generates_compose_node():
    image, elements, boundary = _wechat_fixture()
    draw = ImageDraw.Draw(image)
    left, top, right, bottom = boundary
    draw.line(
        (right - 2, top + (bottom - top) * 0.15, right + 2, top + (bottom - top) * 0.55),
        fill=(250, 250, 250),
        width=5,
    )

    result = detect_wechat_compose_region(
        image,
        elements,
        target_family="wechat",
    )

    assert result.element is None


def test_existing_semantic_edit_suppresses_synthetic_duplicate():
    image, elements, boundary = _wechat_fixture()
    existing = {
        "element_index": 3,
        "control_type": "edit",
        "bbox": [
            boundary[0] + 10,
            boundary[1] + 10,
            boundary[2] - 10,
            boundary[3] - 50,
        ],
    }

    result = detect_wechat_compose_region(
        image,
        elements,
        target_family="wechat",
        existing_nodes=[existing],
    )

    assert result.element is None


def test_merge_adds_compose_to_cache_and_preserves_public_projection(monkeypatch):
    import computer_server.handlers.som_bridge as som_bridge

    image, elements, _ = _wechat_fixture()
    monkeypatch.setattr(
        som_bridge,
        "detect_sync",
        lambda *args, **kwargs: type(
            "VisionResult",
            (),
            {"elements": elements, "metadata": {"stage_timings": {}}},
        )(),
    )

    class FakeCache:
        def append_vision_elements(self, pid, window_id, cached_elements):
            self.elements = list(cached_elements)

        def set_quality(self, pid, window_id, quality):
            self.quality = quality

    cache = FakeCache()
    result = window_state_service.merge_som_vision(
        image,
        pid=1,
        window_id=2,
        screen_width=image.width,
        screen_height=image.height,
        compression_ratio=2.0,
        crop_origin_window_x=4,
        crop_origin_window_y=6,
        cache=cache,
        uia_quality="poor",
        target_family="wechat",
        existing_nodes=[],
    )

    edit_node = next(node for node in result["nodes"] if node["control_type"] == "edit")
    cached_edit = next(element for element in cache.elements if element.control_type == "edit")
    assert edit_node["element_index"] == cached_edit.element_index
    assert result["vision_metadata"]["compose_region_count"] == 1
    assert result["vision_metadata"]["stage_timings"]["compose_layout_latency"] > 0

    enriched = window_state_service.enrich_nodes_with_action_hints(result["nodes"])
    enriched_edit = next(node for node in enriched if node["control_type"] == "edit")
    assert {"click", "press", "focus", "type"}.issubset(
        enriched_edit["action_hints"]
    )
    queried = window_state_service.filter_nodes_by_query(enriched, "message input", False)
    assert [node["element_index"] for node in queried] == [edit_node["element_index"]]

    filler = [
        {
            "element_index": index,
            "source": "vision",
            "control_type": "text",
            "name": f"filler {index}",
            "actions": [],
            "bbox": [0, 0, 1, 1],
            "center_x": 0,
            "center_y": 0,
        }
        for index in range(20000, 20120)
    ]
    limited = window_state_service.limit_nodes_for_response(
        enriched + filler,
        window_state_service.DEFAULT_RETURNED_NODE_LIMIT,
    )
    assert edit_node["element_index"] in {
        node["element_index"] for node in limited
    }

    geometry = window_state_service.WindowSnapshotGeometry(
        full_width=image.width * 2,
        full_height=image.height * 2,
        screenshot_origin_x=0,
        screenshot_origin_y=0,
        window_origin_x=0,
        window_origin_y=0,
        crop_origin_screen_x=4,
        crop_origin_screen_y=6,
        crop_origin_window_x=4,
        crop_origin_window_y=6,
        original_width=image.width * 2,
        original_height=image.height * 2,
        screen_width=image.width,
        screen_height=image.height,
        compression_ratio=2.0,
    )
    projected = window_state_service.project_nodes_to_window_resized(
        [edit_node],
        geometry,
    )[0]
    assert 0 <= projected["bbox"][0] < projected["bbox"][2] <= image.width
    assert 0 <= projected["bbox"][1] < projected["bbox"][3] <= image.height
    assert projected["bbox"][3] < int(image.height * 0.94)


def test_compose_detector_p95_is_below_ten_milliseconds():
    image, elements, _ = _wechat_fixture()
    detect_wechat_compose_region(image, elements, target_family="wechat")

    latencies = [
        detect_wechat_compose_region(
            image,
            elements,
            target_family="wechat",
        ).diagnostics["compose_layout_latency"]
        * 1000
        for _ in range(25)
    ]
    p95 = sorted(latencies)[23]

    assert statistics.median(latencies) < 10.0
    assert p95 < 10.0
