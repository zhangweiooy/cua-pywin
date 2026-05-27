from __future__ import annotations

from computer_server.handlers.zoom_registry import ZoomContext, get_zoom_registry
from computer_server.services.coordinate_service import resolve_from_zoom


def test_zoom_registry_keeps_stable_zoom_ids_for_same_window():
    registry = get_zoom_registry()
    registry.clear_all()
    try:
        registry.set(
            10,
            20,
            ZoomContext(
                zoom_id="zoom-first",
                pid=10,
                window_id=20,
                crop_rect=(100, 200, 300, 400),
                scale_x=2.0,
                scale_y=4.0,
                output_width=400,
                output_height=800,
                padding=0,
            ),
        )
        registry.set(
            10,
            20,
            ZoomContext(
                zoom_id="zoom-second",
                pid=10,
                window_id=20,
                crop_rect=(500, 600, 700, 800),
                scale_x=1.0,
                scale_y=1.0,
                output_width=200,
                output_height=200,
                padding=0,
            ),
        )

        assert registry.unscale_by_id("zoom-first", 20, 40) == (110, 210)
        assert registry.unscale_by_id("zoom-second", 20, 40) == (520, 640)
        assert registry.unscale(10, 20, 20, 40) == (520, 640)
        assert resolve_from_zoom(20, 40, zoom_id="zoom-first") == (110, 210)

        registry.clear(10, 20)
        assert registry.get_by_id("zoom-first") is None
        assert registry.get_by_id("zoom-second") is None
    finally:
        registry.clear_all()
