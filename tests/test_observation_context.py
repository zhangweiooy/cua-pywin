from __future__ import annotations

from computer_server.services.observation_context import ObservationContextRegistry


def test_observation_registry_marks_only_latest_observation_current():
    registry = ObservationContextRegistry()

    first = registry.create_observation(
        pid=1,
        window_id=2,
        node_count=3,
        unfiltered_node_count=4,
        screen_width=100,
        screen_height=50,
        capture_geometry={"compression_ratio": 2.0},
        capture_metadata={"capture_backend": "wgc"},
    )
    second = registry.create_observation(
        pid=1,
        window_id=2,
        node_count=5,
        unfiltered_node_count=6,
    )

    assert registry.get_observation(first.observation_id) == first
    assert registry.get_observation(second.observation_id) == second
    assert registry.is_latest_observation(first.observation_id) is False
    assert registry.is_latest_observation(second.observation_id) is True
    assert registry.get_screenshot(first.screenshot_id) is not None
    assert registry.is_latest_screenshot(first.screenshot_id) is False
    assert registry.is_latest_screenshot(second.screenshot_id) is True

    capture = registry.get_screenshot(first.screenshot_id).to_public_capture()
    assert capture["id"] == first.screenshot_id
    assert capture["observation_id"] == first.observation_id
    assert capture["window"] == {"id": 2, "pid": 1}
    assert capture["screen_width"] == 100
    assert capture["screen_height"] == 50
    assert capture["capture_backend"] == "wgc"


def test_observation_registry_expires_old_observations():
    registry = ObservationContextRegistry(ttl_seconds=-1)
    observation = registry.create_observation(
        pid=1,
        window_id=2,
        node_count=3,
        unfiltered_node_count=4,
    )

    assert registry.get_observation(observation.observation_id) is None
    assert registry.get_screenshot(observation.screenshot_id) is None


def test_observation_registry_registers_related_screenshot():
    registry = ObservationContextRegistry()
    observation = registry.create_observation(
        pid=1,
        window_id=2,
        node_count=0,
        unfiltered_node_count=0,
    )

    related = registry.create_screenshot(
        observation_id=observation.observation_id,
        pid=1,
        window_id=3,
        screen_width=40,
        screen_height=20,
        capture_geometry={"crop_origin_screen_x": 10},
        capture_metadata={"capture_backend": "wgc"},
    )

    assert registry.get_screenshot(related.screenshot_id) == related
    assert registry.is_latest_screenshot(related.screenshot_id) is True
    assert related.observation_id == observation.observation_id
    assert related.window_id == 3
