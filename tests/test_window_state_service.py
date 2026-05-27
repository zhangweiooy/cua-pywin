from __future__ import annotations

import base64
from io import BytesIO

import pytest
from PIL import Image

import computer_server.services.window_state_service as window_state_service
from computer_server.services.window_state_service import (
    filter_nodes_by_query,
    get_window_state_snapshot,
    node_matches_query,
    prepare_window_snapshot_image,
)


def test_prepare_window_snapshot_tracks_partially_offscreen_crop_origin():
    img = Image.new("RGB", (200, 100), "white")
    bounds = {"x": -20, "y": 10, "width": 100, "height": 50}

    cropped, geometry = prepare_window_snapshot_image(
        img,
        bounds,
        screenshot_origin=(0, 0),
        max_image_dimension=1280,
    )

    assert cropped.size == (80, 50)
    assert geometry.window_origin_x == -20
    assert geometry.window_origin_y == 10
    assert geometry.crop_origin_screen_x == 0
    assert geometry.crop_origin_screen_y == 10
    assert geometry.crop_origin_window_x == 20
    assert geometry.crop_origin_window_y == 0
    assert geometry.compression_ratio == 1.0


def test_prepare_window_snapshot_uses_virtual_desktop_origin():
    img = Image.new("RGB", (300, 100), "white")
    bounds = {"x": -50, "y": 10, "width": 100, "height": 50}

    cropped, geometry = prepare_window_snapshot_image(
        img,
        bounds,
        screenshot_origin=(-100, 0),
        max_image_dimension=1280,
    )

    assert cropped.size == (100, 50)
    assert geometry.window_origin_x == -50
    assert geometry.crop_origin_screen_x == -50
    assert geometry.crop_origin_window_x == 0


def test_prepare_window_snapshot_records_resize_metadata():
    img = Image.new("RGB", (2000, 1000), "white")
    bounds = {"x": 0, "y": 0, "width": 2000, "height": 1000}

    resized, geometry = prepare_window_snapshot_image(
        img,
        bounds,
        screenshot_origin=(0, 0),
        max_image_dimension=1000,
    )

    assert resized.size == (1000, 500)
    assert geometry.original_width == 2000
    assert geometry.original_height == 1000
    assert geometry.screen_width == 1000
    assert geometry.screen_height == 500
    assert geometry.compression_ratio == 2.0
    assert geometry.to_zoom_metadata()["compression_ratio"] == 2.0


def test_node_matches_query_checks_common_node_fields():
    node = {
        "source": "uia",
        "control_type": "Button",
        "name": "保存",
        "value": "",
        "actions": ["invoke"],
    }

    assert node_matches_query(node, "保存")
    assert node_matches_query(node, "button")
    assert node_matches_query(node, "INVOKE")
    assert not node_matches_query(node, "cancel")


def test_filter_nodes_by_query_filters_returned_nodes_by_default():
    nodes = [
        {"element_index": 1, "control_type": "Button", "name": "保存"},
        {"element_index": 2, "control_type": "Edit", "name": "搜索"},
    ]

    filtered = filter_nodes_by_query(nodes, "搜索", include_all_nodes=False)

    assert filtered == [nodes[1]]


def test_filter_nodes_by_query_can_return_all_nodes_when_requested():
    nodes = [
        {"element_index": 1, "control_type": "Button", "name": "保存"},
        {"element_index": 2, "control_type": "Edit", "name": "搜索"},
    ]

    filtered = filter_nodes_by_query(nodes, "搜索", include_all_nodes=True)

    assert filtered == nodes
    assert filtered is not nodes


class _FakeAutomationHandler:
    async def screenshot(self, format="jpeg", quality=85):
        img = Image.new("RGB", (20, 20), "white")
        buf = BytesIO()
        img.save(buf, format="JPEG")
        return {
            "success": True,
            "image_data": base64.b64encode(buf.getvalue()).decode("ascii"),
            "origin": {"x": 0, "y": 0},
        }

    async def get_window_bounds(self, pid, window_id):
        return {"x": 0, "y": 0, "width": 20, "height": 20}


@pytest.mark.asyncio
async def test_get_window_state_snapshot_filters_returned_nodes_not_cache(monkeypatch):
    nodes = [
        {
            "element_index": 1,
            "source": "uia",
            "control_type": "Button",
            "name": "保存",
            "center_x": 5,
            "center_y": 5,
            "bbox": [1, 1, 9, 9],
            "actions": ["invoke"],
            "enabled": True,
        },
        {
            "element_index": 2,
            "source": "uia",
            "control_type": "Edit",
            "name": "搜索",
            "center_x": 15,
            "center_y": 15,
            "bbox": [10, 10, 19, 19],
            "actions": ["set_value"],
            "enabled": True,
        },
    ]

    async def fake_collect_uia_nodes(*args, **kwargs):
        return {
            "nodes": list(nodes),
            "total_elements": 2,
            "uia_node_count": 2,
            "tree_markdown": "- 搜索",
            "uia_quality": "ok",
            "tree_quality": "ok",
        }

    monkeypatch.setattr(window_state_service, "collect_uia_nodes", fake_collect_uia_nodes)
    monkeypatch.setattr(window_state_service, "detect_popover_candidates", lambda *args: "")
    monkeypatch.setattr(window_state_service, "save_window_state_screenshot", lambda *args: "shot.jpg")

    result = await get_window_state_snapshot(
        _FakeAutomationHandler(),
        pid=123,
        window_id=456,
        query="搜索",
    )

    assert result["nodes"] == [nodes[1]]
    assert result["node_count"] == 1
    assert result["returned_node_count"] == 1
    assert result["unfiltered_node_count"] == 2
    assert result["merged_node_count"] == 2
    assert result["nodes_filtered"] is True


@pytest.mark.asyncio
async def test_get_window_state_snapshot_can_return_all_nodes_with_query(monkeypatch):
    nodes = [
        {
            "element_index": 1,
            "source": "uia",
            "control_type": "Button",
            "name": "保存",
            "center_x": 5,
            "center_y": 5,
            "bbox": [1, 1, 9, 9],
            "actions": ["invoke"],
            "enabled": True,
        },
        {
            "element_index": 2,
            "source": "uia",
            "control_type": "Edit",
            "name": "搜索",
            "center_x": 15,
            "center_y": 15,
            "bbox": [10, 10, 19, 19],
            "actions": ["set_value"],
            "enabled": True,
        },
    ]

    async def fake_collect_uia_nodes(*args, **kwargs):
        return {
            "nodes": list(nodes),
            "total_elements": 2,
            "uia_node_count": 2,
            "tree_markdown": "- 搜索",
            "uia_quality": "ok",
            "tree_quality": "ok",
        }

    monkeypatch.setattr(window_state_service, "collect_uia_nodes", fake_collect_uia_nodes)
    monkeypatch.setattr(window_state_service, "detect_popover_candidates", lambda *args: "")
    monkeypatch.setattr(window_state_service, "save_window_state_screenshot", lambda *args: "shot.jpg")

    result = await get_window_state_snapshot(
        _FakeAutomationHandler(),
        pid=123,
        window_id=456,
        query="搜索",
        include_all_nodes=True,
    )

    assert result["nodes"] == nodes
    assert result["node_count"] == 2
    assert result["returned_node_count"] == 2
    assert result["unfiltered_node_count"] == 2
    assert result["nodes_filtered"] is False
