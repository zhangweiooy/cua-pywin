"""Per-server observation handles for window snapshots."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Dict, Optional, Tuple


@dataclass(frozen=True)
class SnapshotContext:
    snapshot_id: str
    pid: int
    window_id: int
    node_count: int
    unfiltered_node_count: int
    created_at: float


class ObservationContextRegistry:
    """Stores snapshot handles owned by one MCP server instance."""

    def __init__(self, ttl_seconds: float = 600.0) -> None:
        self._ttl_seconds = ttl_seconds
        self._snapshots: Dict[str, SnapshotContext] = {}
        self._latest_by_window: Dict[Tuple[int, int], str] = {}

    def create_snapshot(
        self,
        pid: int,
        window_id: int,
        node_count: int,
        unfiltered_node_count: int,
    ) -> SnapshotContext:
        self._expire_old()
        snapshot_id = f"snap-{uuid.uuid4().hex}"
        ctx = SnapshotContext(
            snapshot_id=snapshot_id,
            pid=int(pid),
            window_id=int(window_id),
            node_count=int(node_count),
            unfiltered_node_count=int(unfiltered_node_count),
            created_at=time.monotonic(),
        )
        self._snapshots[snapshot_id] = ctx
        self._latest_by_window[(ctx.pid, ctx.window_id)] = snapshot_id
        return ctx

    def get_snapshot(self, snapshot_id: str) -> Optional[SnapshotContext]:
        self._expire_old()
        return self._snapshots.get(snapshot_id)

    def is_latest(self, snapshot_id: str) -> bool:
        ctx = self.get_snapshot(snapshot_id)
        if ctx is None:
            return False
        return self._latest_by_window.get((ctx.pid, ctx.window_id)) == snapshot_id

    def _expire_old(self) -> None:
        now = time.monotonic()
        expired = [
            snapshot_id
            for snapshot_id, ctx in self._snapshots.items()
            if now - ctx.created_at > self._ttl_seconds
        ]
        for snapshot_id in expired:
            ctx = self._snapshots.pop(snapshot_id, None)
            if ctx is not None and self._latest_by_window.get((ctx.pid, ctx.window_id)) == snapshot_id:
                self._latest_by_window.pop((ctx.pid, ctx.window_id), None)
