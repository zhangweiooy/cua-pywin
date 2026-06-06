from __future__ import annotations

from computer_server.services.observation_context import ObservationContextRegistry


def test_observation_registry_marks_only_latest_snapshot_current():
    registry = ObservationContextRegistry()

    first = registry.create_snapshot(
        pid=1,
        window_id=2,
        node_count=3,
        unfiltered_node_count=4,
        screen_width=100,
        screen_height=50,
        capture_geometry={"compression_ratio": 2.0},
        capture_metadata={"capture_backend": "wgc"},
    )
    second = registry.create_snapshot(
        pid=1,
        window_id=2,
        node_count=5,
        unfiltered_node_count=6,
    )

    assert registry.get_snapshot(first.snapshot_id) == first
    assert registry.get_snapshot(second.snapshot_id) == second
    assert registry.is_latest(first.snapshot_id) is False
    assert registry.is_latest(second.snapshot_id) is True
    assert registry.get_screenshot(first.screenshot_id) is not None
    assert registry.is_latest_screenshot(first.screenshot_id) is False
    assert registry.is_latest_screenshot(second.screenshot_id) is True

    capture = registry.get_screenshot(first.screenshot_id).to_public_capture()
    assert capture["id"] == first.screenshot_id
    assert capture["snapshot_id"] == first.snapshot_id
    assert capture["screen_width"] == 100
    assert capture["screen_height"] == 50
    assert capture["capture_backend"] == "wgc"


def test_observation_registry_expires_old_snapshots():
    registry = ObservationContextRegistry(ttl_seconds=-1)
    snapshot = registry.create_snapshot(
        pid=1,
        window_id=2,
        node_count=3,
        unfiltered_node_count=4,
    )

    assert registry.get_snapshot(snapshot.snapshot_id) is None
    assert registry.get_screenshot(snapshot.screenshot_id) is None


def test_observation_registry_registers_related_screenshot():
    registry = ObservationContextRegistry()
    snapshot = registry.create_snapshot(
        pid=1,
        window_id=2,
        node_count=0,
        unfiltered_node_count=0,
    )

    related = registry.create_screenshot(
        snapshot_id=snapshot.snapshot_id,
        pid=1,
        window_id=3,
        screen_width=40,
        screen_height=20,
        capture_geometry={"crop_origin_screen_x": 10},
        capture_metadata={"capture_backend": "wgc"},
    )

    assert registry.get_screenshot(related.screenshot_id) == related
    assert registry.is_latest_screenshot(related.screenshot_id) is True
    assert related.snapshot_id == snapshot.snapshot_id
    assert related.window_id == 3
