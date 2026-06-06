from __future__ import annotations

import base64
from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image

import computer_server.services.window_state_service as window_state_service
from computer_server.handlers.element_cache import CachedElement
from computer_server.services.window_state_service import (
    MSAAWalkGate,
    UIAWalkGate,
    create_zoom_image,
    filter_nodes_by_query,
    get_accessibility_tree_snapshot,
    get_window_state_snapshot,
    node_matches_query,
    prepare_direct_window_snapshot_image,
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


def test_prepare_direct_window_snapshot_uses_window_origin():
    img = Image.new("RGBA", (200, 100), "white")
    bounds = {"x": -20, "y": 10, "width": 200, "height": 100}

    resized, geometry = prepare_direct_window_snapshot_image(
        img,
        bounds,
        max_image_dimension=100,
    )

    assert resized.size == (100, 50)
    assert geometry.window_origin_x == -20
    assert geometry.window_origin_y == 10
    assert geometry.crop_origin_screen_x == -20
    assert geometry.crop_origin_screen_y == 10
    assert geometry.crop_origin_window_x == 0
    assert geometry.crop_origin_window_y == 0
    assert geometry.compression_ratio == 2.0


def test_prepare_direct_window_snapshot_uses_capture_pixels_when_dpi_sizes_differ():
    img = Image.new("RGBA", (1500, 900), "white")
    bounds = {"x": 1920, "y": 100, "width": 1000, "height": 600}

    resized, geometry = prepare_direct_window_snapshot_image(
        img,
        bounds,
        max_image_dimension=1000,
    )

    assert resized.size == (1000, 600)
    assert geometry.window_origin_x == 1920
    assert geometry.window_origin_y == 100
    assert geometry.original_width == 1500
    assert geometry.original_height == 900
    assert geometry.compression_ratio == 1.5


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


def test_node_matches_query_checks_icon_label_metadata():
    node = {
        "source": "vision",
        "control_type": "icon",
        "name": "send button",
        "metadata": {"icon_label": "send button"},
        "actions": ["click"],
    }

    assert node_matches_query(node, "send")
    assert node_matches_query(node, "tinyclip") is False


def test_merge_som_vision_exposes_confidence_on_nodes(monkeypatch):
    import computer_server.handlers.som_bridge as som_bridge

    element = CachedElement(
        element_index=10000,
        source="vision",
        center_x=10,
        center_y=10,
        bbox=(5, 5, 20, 20),
        control_type="text",
        name="搜索",
        metadata={"vision_confidence": 0.88, "ocr_confidence": 0.88},
    )

    monkeypatch.setattr(
        som_bridge,
        "detect_sync",
        lambda *args, **kwargs: SimpleNamespace(
            elements=[element],
            metadata={"stage_timings": {"ocr_latency": 0.01}},
        ),
    )

    class FakeCache:
        def append_vision_elements(self, pid, window_id, elements):
            self.elements = elements

        def set_quality(self, pid, window_id, quality):
            self.quality = quality

    result = window_state_service.merge_som_vision(
        Image.new("RGB", (100, 50), "white"),
        pid=1,
        window_id=2,
        screen_width=100,
        screen_height=50,
        compression_ratio=2.0,
        crop_origin_window_x=3,
        crop_origin_window_y=4,
        cache=FakeCache(),
        uia_quality="poor",
    )

    node = result["nodes"][0]
    assert node["confidence"] == 0.88
    assert node["ocr_confidence"] == 0.88
    assert node["metadata"]["vision_confidence"] == 0.88


class _FakeAutomationHandler:
    async def screenshot(self, format="jpeg", quality=85):
        img = Image.new("RGB", (20, 20), "white")
        buf = BytesIO()
        img.save(buf, format="JPEG")
        return {
            "success": True,
            "image_data": base64.b64encode(buf.getvalue()).decode("ascii"),
            "origin": {"x": 0, "y": 0},
            "virtual_screen": {"x": 0, "y": 0, "width": 20, "height": 20},
            "capture_metadata": {
                "capture_mode": "screen_full",
                "capture_source": "pil_imagegrab",
            },
        }

    async def get_window_bounds(self, pid, window_id):
        return {"x": 0, "y": 0, "width": 20, "height": 20}


class _NoScreenshotAutomationHandler(_FakeAutomationHandler):
    async def screenshot(self, format="jpeg", quality=85):
        raise AssertionError("screen-crop fallback should not run when WGC succeeds")

    async def get_window_bounds(self, pid, window_id):
        return {"x": 5, "y": 7, "width": 30, "height": 20}


class _ZoomNoScreenshotAutomationHandler(_FakeAutomationHandler):
    async def screenshot(self, format="jpeg", quality=85):
        raise AssertionError("screen-crop zoom fallback should not run when WGC succeeds")

    async def get_window_bounds(self, pid, window_id):
        return {"x": 10, "y": 20, "width": 40, "height": 30}


@pytest.mark.asyncio
async def test_get_window_state_snapshot_prefers_wgc_capture(monkeypatch):
    async def fake_capture_window_with_wgc(window_id, *, timeout_s=1.0):
        from computer_server.handlers.windows_wgc import WgcWindowCapture

        return WgcWindowCapture(
            image=Image.new("RGBA", (30, 20), "white"),
            metadata={
                "capture_backend": "wgc",
                "capture_source": "windows_graphics_capture",
                "capture_mode": "wgc_window",
                "occlusion_safe": True,
                "occlusion_risk": False,
                "stage_timings": {
                    "wgc_frame_wait_latency": 0.01,
                    "wgc_total_latency": 0.02,
                },
            },
        )

    async def fake_collect_uia_nodes(*args, **kwargs):
        return {
            "nodes": [],
            "total_elements": 0,
            "uia_node_count": 0,
            "tree_markdown": "",
            "uia_quality": "empty",
            "tree_quality": "empty",
        }

    async def fake_collect_msaa_nodes(*args, **kwargs):
        return {
            "nodes": [],
            "total_elements": 0,
            "msaa_node_count": 0,
            "tree_markdown": "",
            "msaa_quality": "empty",
        }

    import computer_server.handlers.windows_wgc as windows_wgc

    monkeypatch.setattr(windows_wgc, "capture_window_with_wgc", fake_capture_window_with_wgc)
    monkeypatch.setattr(window_state_service, "collect_uia_nodes", fake_collect_uia_nodes)
    monkeypatch.setattr(window_state_service, "collect_msaa_nodes", fake_collect_msaa_nodes)
    monkeypatch.setattr(window_state_service, "detect_popover_candidates", lambda *args: "")
    monkeypatch.setattr(window_state_service, "save_window_state_screenshot", lambda *args: "shot.jpg")

    result = await get_window_state_snapshot(
        _NoScreenshotAutomationHandler(),
        pid=123,
        window_id=456,
    )

    assert result["success"] is True
    assert result["screen_width"] == 30
    assert result["screen_height"] == 20
    assert result["screenshot_origin"] == {"x": 5, "y": 7}
    assert result["capture_metadata"]["capture_backend"] == "wgc"
    assert result["capture_metadata"]["capture_mode"] == "wgc_window"
    assert result["capture_metadata"]["occlusion_safe"] is True
    assert result["capture_metadata"]["occlusion_risk"] is False
    timings = result["capture_metadata"]["stage_timings"]
    assert timings["wgc_frame_wait_latency"] == 0.01
    assert timings["wgc_total_latency"] == 0.02
    assert timings["wgc_latency"] >= 0
    assert timings["crop_resize_latency"] >= 0
    assert timings["capture_total_latency"] >= 0


@pytest.mark.asyncio
async def test_get_window_state_snapshot_reports_minimized_wgc_fallback_timings(
    monkeypatch,
):
    async def fake_capture_window_with_wgc(window_id, *, timeout_s=1.0):
        raise RuntimeError("target_window_minimized")

    async def fake_collect_uia_nodes(*args, **kwargs):
        return {
            "nodes": [],
            "total_elements": 0,
            "uia_node_count": 0,
            "tree_markdown": "",
            "uia_quality": "ok",
            "tree_quality": "ok",
        }

    async def fake_collect_msaa_nodes(*args, **kwargs):
        return {
            "nodes": [],
            "total_elements": 0,
            "msaa_node_count": 0,
            "tree_markdown": "",
            "msaa_quality": "empty",
        }

    import computer_server.handlers.windows_wgc as windows_wgc

    monkeypatch.setattr(windows_wgc, "capture_window_with_wgc", fake_capture_window_with_wgc)
    monkeypatch.setattr(
        window_state_service,
        "inspect_window_capture_state",
        lambda pid, window_id, bounds: {
            "pid": pid,
            "window_id": window_id,
            "is_minimized": True,
            "is_visible": True,
            "bounds": bounds,
        },
    )
    monkeypatch.setattr(window_state_service, "collect_uia_nodes", fake_collect_uia_nodes)
    monkeypatch.setattr(window_state_service, "collect_msaa_nodes", fake_collect_msaa_nodes)
    monkeypatch.setattr(window_state_service, "detect_popover_candidates", lambda *args: "")
    monkeypatch.setattr(window_state_service, "save_window_state_screenshot", lambda *args: "shot.jpg")

    result = await get_window_state_snapshot(
        _FakeAutomationHandler(),
        pid=123,
        window_id=456,
    )

    metadata = result["capture_metadata"]
    assert metadata["capture_backend"] == "screen_crop"
    assert metadata["fallback"] == {
        "from_backend": "wgc",
        "reason": "target_window_minimized",
    }
    assert "target_window_minimized" in metadata["warnings"]
    assert "preferred_capture_backend_failed" in metadata["warnings"]
    timings = metadata["stage_timings"]
    assert timings["wgc_latency"] >= 0
    assert timings["screen_capture_latency"] >= 0
    assert timings["image_decode_latency"] >= 0
    assert timings["crop_resize_latency"] >= 0
    assert timings["capture_total_latency"] >= 0


@pytest.mark.asyncio
async def test_create_zoom_image_uses_wgc_backend(monkeypatch):
    async def fake_capture_window_with_wgc(window_id, *, timeout_s=1.0):
        from computer_server.handlers.windows_wgc import WgcWindowCapture

        return WgcWindowCapture(
            image=Image.new("RGBA", (40, 30), "white"),
            metadata={"capture_backend": "wgc", "latency_ms": 12.0},
        )

    import computer_server.handlers.windows_wgc as windows_wgc

    monkeypatch.setattr(windows_wgc, "capture_window_with_wgc", fake_capture_window_with_wgc)
    zoom_metadata = {
        (123, 456): {
            "compression_ratio": 2.0,
            "original_width": 40,
            "original_height": 30,
            "window_origin_x": 5,
            "window_origin_y": 7,
            "crop_origin_screen_x": 5,
            "crop_origin_screen_y": 7,
            "capture_backend": "wgc",
        }
    }

    result = await create_zoom_image(
        _ZoomNoScreenshotAutomationHandler(),
        pid=123,
        window_id=456,
        x1=5,
        y1=5,
        x2=10,
        y2=10,
        zoom_metadata=zoom_metadata,
    )

    assert result["success"] is True
    assert result["capture_backend"] == "wgc"
    assert result["capture_metadata"]["occlusion_safe"] is True
    assert result["crop_rect"] == [18, 28, 32, 42]


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

    async def fake_collect_msaa_nodes(*args, **kwargs):
        return {
            "nodes": [],
            "total_elements": 0,
            "msaa_node_count": 0,
            "tree_markdown": "",
            "msaa_quality": "empty",
        }

    monkeypatch.setattr(window_state_service, "collect_uia_nodes", fake_collect_uia_nodes)
    monkeypatch.setattr(window_state_service, "collect_msaa_nodes", fake_collect_msaa_nodes)
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
    assert result["capture_metadata"]["capture_mode"] == "screen_crop"
    assert result["capture_metadata"]["occlusion_risk"] is True
    assert result["node_sources"] == ["uia"]


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

    async def fake_collect_msaa_nodes(*args, **kwargs):
        return {
            "nodes": [],
            "total_elements": 0,
            "msaa_node_count": 0,
            "tree_markdown": "",
            "msaa_quality": "empty",
        }

    monkeypatch.setattr(window_state_service, "collect_uia_nodes", fake_collect_uia_nodes)
    monkeypatch.setattr(window_state_service, "collect_msaa_nodes", fake_collect_msaa_nodes)
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


@pytest.mark.asyncio
async def test_get_window_state_snapshot_merges_msaa_when_uia_is_poor(monkeypatch):
    async def fake_collect_uia_nodes(*args, **kwargs):
        return {
            "nodes": [],
            "total_elements": 1,
            "uia_node_count": 0,
            "tree_markdown": "",
            "uia_quality": "empty",
            "tree_quality": "empty",
        }

    async def fake_collect_msaa_nodes(*args, **kwargs):
        return {
            "nodes": [
                {
                    "element_index": 0,
                    "source": "msaa",
                    "control_type": "Button",
                    "role": "Button",
                    "name": "确定",
                    "center_x": 10,
                    "center_y": 10,
                    "bbox": [5, 5, 15, 15],
                    "actions": ["invoke"],
                    "enabled": True,
                    "depth": 1,
                }
            ],
            "total_elements": 3,
            "msaa_node_count": 1,
            "tree_markdown": '[0] Button "确定" [actions=[invoke]]',
            "msaa_quality": "ok",
        }

    monkeypatch.setattr(window_state_service, "collect_uia_nodes", fake_collect_uia_nodes)
    monkeypatch.setattr(window_state_service, "collect_msaa_nodes", fake_collect_msaa_nodes)
    monkeypatch.setattr(window_state_service, "detect_popover_candidates", lambda *args: "")
    monkeypatch.setattr(window_state_service, "save_window_state_screenshot", lambda *args: "shot.jpg")

    result = await get_window_state_snapshot(
        _FakeAutomationHandler(),
        pid=123,
        window_id=456,
    )

    assert result["success"] is True
    assert result["nodes"][0]["source"] == "msaa"
    assert result["nodes"][0]["name"] == "确定"
    assert result["uia_quality"] == "empty"
    assert result["msaa_quality"] == "ok"
    assert result["tree_quality"] == "ok"
    assert result["msaa_node_count"] == 1
    assert result["node_sources"] == ["msaa"]


@pytest.mark.asyncio
async def test_msaa_without_indexed_nodes_does_not_suppress_vision(monkeypatch):
    vision_called = False

    async def fake_collect_uia_nodes(*args, **kwargs):
        return {
            "nodes": [],
            "total_elements": 1,
            "uia_node_count": 0,
            "tree_markdown": "",
            "uia_quality": "poor",
            "tree_quality": "poor",
        }

    async def fake_collect_msaa_nodes(*args, **kwargs):
        return {
            "nodes": [],
            "total_elements": 50,
            "msaa_node_count": 0,
            "tree_markdown": 'Button "确定"',
            "msaa_quality": "ok",
        }

    def fake_merge_som_vision(*args, **kwargs):
        nonlocal vision_called
        vision_called = True
        return {
            "nodes": [],
            "vision_node_count": 0,
            "vision_quality": "empty",
            "vision_metadata": {},
        }

    monkeypatch.setattr(window_state_service, "collect_uia_nodes", fake_collect_uia_nodes)
    monkeypatch.setattr(window_state_service, "collect_msaa_nodes", fake_collect_msaa_nodes)
    monkeypatch.setattr(window_state_service, "merge_som_vision", fake_merge_som_vision)
    monkeypatch.setattr(window_state_service, "detect_popover_candidates", lambda *args: "")
    monkeypatch.setattr(window_state_service, "save_window_state_screenshot", lambda *args: "shot.jpg")

    result = await get_window_state_snapshot(
        _FakeAutomationHandler(),
        pid=123,
        window_id=456,
    )

    assert result["success"] is True
    assert result["tree_quality"] == "poor"
    assert result["msaa_quality"] == "ok"
    assert result["msaa_node_count"] == 0
    assert "MSAA Fallback" not in result["tree_markdown"]
    assert result["tree_markdown"] == 'Button "确定"'
    assert vision_called is True


@pytest.mark.asyncio
async def test_poor_uia_runs_msaa_and_vision(monkeypatch):
    msaa_called = False
    vision_called = False

    async def fake_collect_uia_nodes(*args, **kwargs):
        return {
            "nodes": [],
            "total_elements": 1,
            "uia_node_count": 0,
            "tree_markdown": "",
            "uia_quality": "poor",
            "tree_quality": "poor",
        }

    async def fake_collect_msaa_nodes(*args, **kwargs):
        nonlocal msaa_called
        msaa_called = True
        return {
            "nodes": [],
            "total_elements": 1,
            "msaa_node_count": 0,
            "tree_markdown": "",
            "msaa_quality": "empty",
        }

    def fake_merge_som_vision(*args, **kwargs):
        nonlocal vision_called
        vision_called = True
        return {
            "nodes": [],
            "vision_node_count": 0,
            "vision_quality": "empty",
            "vision_metadata": {},
        }

    monkeypatch.setattr(window_state_service, "collect_uia_nodes", fake_collect_uia_nodes)
    monkeypatch.setattr(window_state_service, "collect_msaa_nodes", fake_collect_msaa_nodes)
    monkeypatch.setattr(window_state_service, "merge_som_vision", fake_merge_som_vision)
    monkeypatch.setattr(window_state_service, "detect_popover_candidates", lambda *args: "")
    monkeypatch.setattr(window_state_service, "save_window_state_screenshot", lambda *args: "shot.jpg")

    result = await get_window_state_snapshot(
        _FakeAutomationHandler(),
        pid=123,
        window_id=456,
    )

    assert result["success"] is True
    assert msaa_called is True
    assert vision_called is True


@pytest.mark.asyncio
async def test_collect_msaa_nodes_preserves_role_metadata(monkeypatch):
    fake_node = SimpleNamespace(
        element_index=7,
        role="ButtonDropDown",
        role_id=0x38,
        name="字体颜色",
        value=None,
        description=None,
        center_x=40,
        center_y=20,
        bbox=(20, 10, 60, 30),
        actions=["invoke", "expand"],
        default_action="按",
        state=0,
        child_id=0,
        depth=2,
    )
    fake_result = SimpleNamespace(
        indexed_nodes=[fake_node],
        nodes=[fake_node],
        total_elements=1,
        tree_markdown='[7] ButtonDropDown "字体颜色" [actions=[invoke,expand]]',
        tree_quality="poor",
    )

    def fake_walk_msaa_tree(*args, **kwargs):
        return fake_result

    import computer_server.handlers.windows_msaa as windows_msaa

    monkeypatch.setattr(windows_msaa, "walk_msaa_tree", fake_walk_msaa_tree)

    result = await window_state_service.collect_msaa_nodes(
        window_id=123,
        query=None,
        win_origin_x=10,
        win_origin_y=5,
        index_start=7,
    )

    node = result["nodes"][0]
    assert node["source"] == "msaa"
    assert node["actions"] == ["invoke", "expand"]
    assert node["bbox"] == [10, 5, 50, 25]
    assert node["metadata"]["msaa_role"] == "ButtonDropDown"
    assert node["metadata"]["msaa_role_id"] == 0x38
    assert node["metadata"]["msaa_role_hex"] == "0x38"
    assert node["metadata"]["msaa_has_location"] is True


@pytest.mark.asyncio
async def test_collect_msaa_nodes_uses_gate_after_timeout(monkeypatch):
    import time

    calls = {"count": 0}

    def slow_walk_msaa_tree(*args, **kwargs):
        calls["count"] += 1
        time.sleep(0.2)
        return SimpleNamespace(
            indexed_nodes=[],
            nodes=[],
            total_elements=0,
            tree_markdown="",
            tree_quality="empty",
        )

    import computer_server.handlers.windows_msaa as windows_msaa

    monkeypatch.setattr(windows_msaa, "walk_msaa_tree", slow_walk_msaa_tree)
    gate = MSAAWalkGate(unhealthy_cooldown_s=5.0)

    first = await window_state_service.collect_msaa_nodes(
        window_id=123,
        query=None,
        win_origin_x=0,
        win_origin_y=0,
        gate=gate,
        pid=456,
        timeout_s=0.01,
    )
    second = await window_state_service.collect_msaa_nodes(
        window_id=123,
        query=None,
        win_origin_x=0,
        win_origin_y=0,
        gate=gate,
        pid=456,
        timeout_s=0.01,
    )

    assert first["msaa_quality"] == "empty"
    assert second["msaa_quality"] == "empty"
    assert calls["count"] == 1


@pytest.mark.asyncio
async def test_get_accessibility_tree_snapshot_uses_uia_gate(monkeypatch):
    fake_result = SimpleNamespace(
        tree_markdown='[0] Button "确定"',
        tree_quality="poor",
        total_elements=1,
    )

    def fake_walk_uia_tree(hwnd):
        assert hwnd == 123
        return fake_result

    import win32process
    import computer_server.handlers.windows_uia as windows_uia

    monkeypatch.setattr(win32process, "GetWindowThreadProcessId", lambda hwnd: (1, 456))
    monkeypatch.setattr(windows_uia, "walk_uia_tree", fake_walk_uia_tree)

    result = await get_accessibility_tree_snapshot(
        123,
        gate=UIAWalkGate(),
        timeout_s=0.5,
    )

    assert result == {
        "success": True,
        "tree_markdown": '[0] Button "确定"',
        "tree_quality": "poor",
        "node_count": 1,
        "window_id": 123,
        "pid": 456,
    }
