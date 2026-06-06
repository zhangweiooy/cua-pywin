from __future__ import annotations

import math

import pytest

import computer_server.overlay as overlay


class _CursorState:
    heading = 1.25


class _Cursor:
    _state = _CursorState()


@pytest.mark.asyncio
async def test_mcp_cursor_animation_uses_lazy_initialized_module_cursor(monkeypatch):
    import computer_server.mcp_server as mcp_server

    calls = []

    class _LazyCursor:
        def move_to(self, x, y, heading):
            calls.append((x, y, heading))

    cursor = _LazyCursor()

    def fake_init():
        overlay._agent_cursor = cursor
        return cursor

    monkeypatch.setattr(overlay, "_agent_cursor", None)
    monkeypatch.setattr(overlay, "init", fake_init)
    with overlay._last_target_lock:
        overlay._last_target_x = None
        overlay._last_target_y = None

    await mcp_server._animate_cursor_to(7, 8)

    assert calls == [(7, 8, pytest.approx(math.pi))]


@pytest.mark.asyncio
async def test_mcp_cursor_animation_is_hidden_when_suppressed(monkeypatch):
    import computer_server.mcp_server as mcp_server

    calls = []

    class _HiddenCursor:
        def move_to(self, x, y, heading):
            calls.append(("move", x, y, heading))

        def set_initial_position(self, x, y, heading=0.785):
            calls.append(("position", x, y))

        def hide(self):
            calls.append(("hide",))

    monkeypatch.setattr(overlay, "_agent_cursor", _HiddenCursor())

    with overlay.suppress_animation():
        assert overlay.is_animation_suppressed() is True
        await mcp_server._animate_cursor_to(17, 18)

    assert overlay.is_animation_suppressed() is False
    assert calls == [("hide",), ("position", 17, 18), ("hide",)]


def test_compute_heading_and_update_reuses_current_heading_for_same_target(monkeypatch):
    monkeypatch.setattr(overlay, "_agent_cursor", _Cursor())
    with overlay._last_target_lock:
        overlay._last_target_x = 10.0
        overlay._last_target_y = 20.0

    heading = overlay._compute_heading_and_update(10, 20)

    assert heading == pytest.approx(1.25)
    with overlay._last_target_lock:
        assert overlay._last_target_x == 10.0
        assert overlay._last_target_y == 20.0


def test_compute_heading_and_update_sets_target_under_lock(monkeypatch):
    monkeypatch.setattr(overlay, "_agent_cursor", _Cursor())
    with overlay._last_target_lock:
        overlay._last_target_x = 10.0
        overlay._last_target_y = 20.0

    heading = overlay._compute_heading_and_update(13, 24)

    assert heading == pytest.approx(math.atan2(4, 3) + math.pi)
    with overlay._last_target_lock:
        assert overlay._last_target_x == 13.0
        assert overlay._last_target_y == 24.0
