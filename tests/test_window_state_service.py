from __future__ import annotations

from PIL import Image

from computer_server.services.window_state_service import prepare_window_snapshot_image


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
