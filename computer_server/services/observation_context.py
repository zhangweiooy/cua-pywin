"""Per-server observation handles for window snapshots."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple


@dataclass(frozen=True)
class SnapshotContext:
    snapshot_id: str
    screenshot_id: str
    pid: int
    window_id: int
    node_count: int
    unfiltered_node_count: int
    created_at: float


@dataclass(frozen=True)
class ScreenshotContext:
    screenshot_id: str
    snapshot_id: str
    pid: int
    window_id: int
    screen_width: int
    screen_height: int
    capture_geometry: Dict[str, Any]
    capture_metadata: Dict[str, Any]
    created_at: float

    def to_public_capture(self) -> Dict[str, Any]:
        capture_backend = self.capture_metadata.get("capture_backend")
        return {
            "id": self.screenshot_id,
            "screenshot_id": self.screenshot_id,
            "snapshot_id": self.snapshot_id,
            "pid": self.pid,
            "window_id": self.window_id,
            "coordinate_space": "window_resized",
            "screen_width": self.screen_width,
            "screen_height": self.screen_height,
            "capture_backend": capture_backend,
            "capture_metadata": dict(self.capture_metadata),
        }


class ObservationContextRegistry:
    """Stores snapshot handles owned by one MCP server instance."""

    def __init__(self, ttl_seconds: float = 600.0) -> None:
        self._ttl_seconds = ttl_seconds
        self._snapshots: Dict[str, SnapshotContext] = {}
        self._screenshots: Dict[str, ScreenshotContext] = {}
        self._latest_by_window: Dict[Tuple[int, int], str] = {}
        self._latest_screenshot_by_window: Dict[Tuple[int, int], str] = {}

    def create_snapshot(
        self,
        pid: int,
        window_id: int,
        node_count: int,
        unfiltered_node_count: int,
        screen_width: int = 0,
        screen_height: int = 0,
        capture_geometry: Optional[Dict[str, Any]] = None,
        capture_metadata: Optional[Dict[str, Any]] = None,
    ) -> SnapshotContext:
        self._expire_old()
        snapshot_id = f"snap-{uuid.uuid4().hex}"
        screenshot_id = f"shot-{uuid.uuid4().hex}"
        created_at = time.monotonic()
        ctx = SnapshotContext(
            snapshot_id=snapshot_id,
            screenshot_id=screenshot_id,
            pid=int(pid),
            window_id=int(window_id),
            node_count=int(node_count),
            unfiltered_node_count=int(unfiltered_node_count),
            created_at=created_at,
        )
        screenshot = ScreenshotContext(
            screenshot_id=screenshot_id,
            snapshot_id=snapshot_id,
            pid=ctx.pid,
            window_id=ctx.window_id,
            screen_width=int(screen_width or 0),
            screen_height=int(screen_height or 0),
            capture_geometry=dict(capture_geometry or {}),
            capture_metadata=dict(capture_metadata or {}),
            created_at=created_at,
        )
        self._snapshots[snapshot_id] = ctx
        self._screenshots[screenshot_id] = screenshot
        self._latest_by_window[(ctx.pid, ctx.window_id)] = snapshot_id
        self._latest_screenshot_by_window[(ctx.pid, ctx.window_id)] = screenshot_id
        return ctx

    def get_snapshot(self, snapshot_id: str) -> Optional[SnapshotContext]:
        self._expire_old()
        return self._snapshots.get(snapshot_id)

    def get_screenshot(self, screenshot_id: str) -> Optional[ScreenshotContext]:
        self._expire_old()
        return self._screenshots.get(screenshot_id)

    def is_latest(self, snapshot_id: str) -> bool:
        ctx = self.get_snapshot(snapshot_id)
        if ctx is None:
            return False
        return self._latest_by_window.get((ctx.pid, ctx.window_id)) == snapshot_id

    def is_latest_screenshot(self, screenshot_id: str) -> bool:
        ctx = self.get_screenshot(screenshot_id)
        if ctx is None:
            return False
        return self._latest_screenshot_by_window.get((ctx.pid, ctx.window_id)) == screenshot_id

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
            if ctx is not None:
                self._screenshots.pop(ctx.screenshot_id, None)
                if self._latest_screenshot_by_window.get((ctx.pid, ctx.window_id)) == ctx.screenshot_id:
                    self._latest_screenshot_by_window.pop((ctx.pid, ctx.window_id), None)

        expired_screenshots = [
            screenshot_id
            for screenshot_id, ctx in self._screenshots.items()
            if now - ctx.created_at > self._ttl_seconds
        ]
        for screenshot_id in expired_screenshots:
            ctx = self._screenshots.pop(screenshot_id, None)
            if ctx is not None and self._latest_screenshot_by_window.get((ctx.pid, ctx.window_id)) == screenshot_id:
                self._latest_screenshot_by_window.pop((ctx.pid, ctx.window_id), None)
