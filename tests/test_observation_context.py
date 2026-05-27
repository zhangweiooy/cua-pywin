from __future__ import annotations

from computer_server.services.observation_context import ObservationContextRegistry


def test_observation_registry_marks_only_latest_snapshot_current():
    registry = ObservationContextRegistry()

    first = registry.create_snapshot(
        pid=1,
        window_id=2,
        node_count=3,
        unfiltered_node_count=4,
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


def test_observation_registry_expires_old_snapshots():
    registry = ObservationContextRegistry(ttl_seconds=-1)
    snapshot = registry.create_snapshot(
        pid=1,
        window_id=2,
        node_count=3,
        unfiltered_node_count=4,
    )

    assert registry.get_snapshot(snapshot.snapshot_id) is None
