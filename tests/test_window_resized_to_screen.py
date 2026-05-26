"""Tests for ``_window_resized_to_screen`` coordinate translation.

The helper converts ``(x, y)`` from the **resized window screenshot** that
``computer_get_window_state`` returns back into absolute screen coordinates.

The bug being fixed: the previous formula was

    int((origin_x + x) * ratio)

which incorrectly multiplied the window origin by the resize ratio. The
correct formula is

    origin_x + int(x * ratio)

because the screenshot is cropped to the window first (origin already in
screen space) and only the cropped region is then resized.

These tests do NOT exercise any real Windows GUI; they monkeypatch the
``ElementCache`` singleton and the ``_get_resize_ratio`` lookup to inject
known values, then assert the arithmetic of the helper directly.
"""

from __future__ import annotations

from typing import Tuple

import pytest

from computer_server.mcp_server import _window_resized_to_screen


@pytest.fixture
def fake_window(monkeypatch):
    """Return a setter that patches the cache origin and resize ratio.

    Usage:
        fake_window(origin=(200, 100), ratio=2.0)
        x, y = _window_resized_to_screen(pid=42, window_id=1, x=10, y=5)
    """

    def _setter(origin: Tuple[int, int], ratio: float) -> None:
        # 1. Patch the resize ratio lookup directly on the mcp_server module.
        monkeypatch.setattr(
            "computer_server.mcp_server._get_resize_ratio",
            lambda pid, window_id: ratio,
        )

        # 2. Patch the element cache singleton's get_window_origin.
        #    The helper does ``from ... import get_element_cache`` *inside*
        #    the function, so we need to patch the source module attribute.
        class _FakeCache:
            def get_window_origin(self, pid: int, window_id: int) -> Tuple[int, int]:
                return origin

            def get_window_origin_optional(self, pid, window_id):
                return self.get_window_origin(pid, window_id)

        monkeypatch.setattr(
            "computer_server.handlers.element_cache.get_element_cache",
            lambda: _FakeCache(),
        )

    return _setter


class TestWindowResizedToScreen:
    """Exercises the screen-space conversion in isolation."""

    def test_origin_zero_ratio_one_is_identity(self, fake_window):
        """origin=(0,0) and ratio=1.0 → output equals input."""
        fake_window(origin=(0, 0), ratio=1.0)
        assert _window_resized_to_screen(pid=1, window_id=1, x=42, y=37) == (42, 37)

    def test_origin_zero_ratio_two_scales_only_xy(self, fake_window):
        """origin=(0,0), ratio=2.0 → coords double, origin contributes 0."""
        fake_window(origin=(0, 0), ratio=2.0)
        assert _window_resized_to_screen(pid=1, window_id=1, x=10, y=20) == (20, 40)

    def test_positive_origin_ratio_one_offsets_only(self, fake_window):
        """origin=(200,100), ratio=1.0 → output = origin + (x, y)."""
        fake_window(origin=(200, 100), ratio=1.0)
        assert _window_resized_to_screen(pid=1, window_id=1, x=10, y=5) == (210, 105)

    def test_positive_origin_ratio_two_does_not_multiply_origin(self, fake_window):
        """origin=(200,100), ratio=2.0, x=10, y=5.

        Previous (buggy) formula: int((200 + 10) * 2) = 420
                                  int((100 + 5) * 2)  = 210
        Correct formula:           200 + int(10 * 2) = 220
                                   100 + int(5 * 2)  = 110
        """
        fake_window(origin=(200, 100), ratio=2.0)
        assert _window_resized_to_screen(pid=1, window_id=1, x=10, y=5) == (220, 110)

    def test_negative_origin_multimonitor_ratio_one(self, fake_window):
        """Window on left monitor (negative origin). Ratio=1.0 → translate only."""
        fake_window(origin=(-100, 50), ratio=1.0)
        assert _window_resized_to_screen(pid=1, window_id=1, x=30, y=40) == (-70, 90)

    def test_negative_origin_multimonitor_ratio_two(self, fake_window):
        """Window on left monitor (negative origin). Ratio=2.0.

        Previous (buggy) formula: int((-100 + 30) * 2) = -140
        Correct formula:           -100 + int(30 * 2) = -40
        """
        fake_window(origin=(-100, 50), ratio=2.0)
        assert _window_resized_to_screen(pid=1, window_id=1, x=30, y=40) == (-40, 130)

    def test_pid_none_returns_input_unchanged(self, fake_window):
        """When pid is None, the helper short-circuits and returns (x, y)."""
        # Even with patched origin, the early-return path should bypass it.
        fake_window(origin=(999, 999), ratio=99.0)
        assert _window_resized_to_screen(pid=None, window_id=1, x=7, y=8) == (7, 8)

    def test_window_id_none_returns_input_unchanged(self, fake_window):
        """When window_id is None, the helper short-circuits."""
        fake_window(origin=(999, 999), ratio=99.0)
        assert _window_resized_to_screen(pid=42, window_id=None, x=7, y=8) == (7, 8)

    def test_zero_input_with_offset(self, fake_window):
        """(x=0, y=0) → output equals origin verbatim (no rounding artefacts)."""
        fake_window(origin=(150, -25), ratio=2.5)
        assert _window_resized_to_screen(pid=1, window_id=1, x=0, y=0) == (150, -25)

    def test_crop_origin_preferred_over_element_cache(self, monkeypatch):
        """When _zoom_metadata has crop_origin, it wins over element cache.

        Scenario: a multi-monitor window starts at screen x=-100 but the
        visible crop starts at screen x=0 (clamped).  The resized
        screenshot the agent sees starts at x=0, so a click at
        resized-image x=10 with ratio 2.0 should land at screen x=20,
        NOT at x = -100 + 20 = -80.
        """
        # Populate _zoom_metadata with a crop origin that differs from the
        # real window origin (which the element cache would return).
        monkeypatch.setattr(
            "computer_server.mcp_server._zoom_metadata",
            {
                (42, 1): {
                    "compression_ratio": 2.0,
                    "crop_origin_screen_x": 0,
                    "crop_origin_screen_y": 50,
                }
            },
        )
        # Even if element cache returns real origin (-100, 50), the
        # helper should use crop_origin_screen (0, 50) from metadata.
        class _FakeCache:
            def get_window_origin(self, pid, window_id):
                return (-100, 50)

            def get_window_origin_optional(self, pid, window_id):
                return self.get_window_origin(pid, window_id)
        monkeypatch.setattr(
            "computer_server.handlers.element_cache.get_element_cache",
            lambda: _FakeCache(),
        )
        # (0 + int(10*2), 50 + int(5*2)) = (20, 60)
        assert _window_resized_to_screen(pid=42, window_id=1, x=10, y=5) == (20, 60)

    def test_fallback_to_element_cache_when_no_zoom_metadata(self, fake_window):
        """When _zoom_metadata has no entry for this (pid, window_id),
        the element cache fallback path is used as before."""
        fake_window(origin=(200, 100), ratio=1.5)
        assert _window_resized_to_screen(pid=1, window_id=1, x=10, y=5) == (215, 107)

    def test_zero_zoom_metadata_ignored(self, monkeypatch):
        """An empty _zoom_metadata dict means no crop origin — fallback to
        element cache even though meta={} is truthy."""
        # _zoom_metadata has no key for (9, 9); the helper falls through.
        class _FakeCache:
            def get_window_origin(self, pid, window_id):
                return (300, 400)

            def get_window_origin_optional(self, pid, window_id):
                return self.get_window_origin(pid, window_id)
        monkeypatch.setattr(
            "computer_server.handlers.element_cache.get_element_cache",
            lambda: _FakeCache(),
        )
        monkeypatch.setattr(
            "computer_server.mcp_server._get_resize_ratio",
            lambda pid, window_id: 1.0,
        )
        assert _window_resized_to_screen(pid=9, window_id=9, x=7, y=3) == (307, 403)

    def test_fractional_ratio_truncates_toward_zero(self, fake_window):
        """``int()`` truncates; verify no drift around the origin.

        With ratio=1.5 and x=3, int(3 * 1.5) = 4 (int truncates 4.5 → 4 on
        Python's int()). origin_x is added afterwards.
        """
        fake_window(origin=(10, 20), ratio=1.5)
        assert _window_resized_to_screen(pid=1, window_id=1, x=3, y=3) == (14, 24)
