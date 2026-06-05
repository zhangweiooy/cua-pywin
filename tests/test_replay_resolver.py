from __future__ import annotations

from computer_server.handlers.element_cache import CachedElement, get_element_cache
from computer_server.services import replay_resolver


def test_resolve_replay_click_adjusts_coordinates_for_moved_window(monkeypatch):
    monkeypatch.setattr(
        replay_resolver,
        "resolve_live_window",
        lambda source: (200, (300, 400, 800, 900)),
    )

    args = {"x": 120, "y": 130, "window_title": "old", "process_name": "app.exe"}
    window_ref = {
        "window_id": 100,
        "origin_x": 100,
        "origin_y": 100,
        "window_title": "old",
        "process_name": "app.exe",
    }

    resolved = replay_resolver.resolve_replay_args(
        "computer_click",
        args,
        window_ref=window_ref,
    )

    assert resolved == {"x": 320, "y": 430, "window_id": "200"}
    assert args["x"] == 120


def test_resolve_replay_drag_adjusts_both_points(monkeypatch):
    monkeypatch.setattr(
        replay_resolver,
        "resolve_live_window",
        lambda source: (100, (50, 60, 500, 600)),
    )

    resolved = replay_resolver.resolve_replay_args(
        "computer_drag",
        {"start_x": 110, "start_y": 120, "end_x": 150, "end_y": 180},
        window_ref={"window_id": 100, "origin_x": 100, "origin_y": 100},
    )

    assert resolved == {
        "start_x": 60,
        "start_y": 80,
        "end_x": 100,
        "end_y": 140,
    }


def test_resolve_replay_mouse_move_adjusts_coordinates_without_window_id(monkeypatch):
    monkeypatch.setattr(
        replay_resolver,
        "resolve_live_window",
        lambda source: (200, (300, 400, 800, 900)),
    )

    resolved = replay_resolver.resolve_replay_args(
        "computer_mouse",
        {"action": "move", "x": 120, "y": 130},
        window_ref={"window_id": 100, "origin_x": 100, "origin_y": 100},
    )

    assert resolved == {"action": "move", "x": 320, "y": 430}


def test_resolve_replay_mouse_down_preserves_current_cursor_mode(monkeypatch):
    monkeypatch.setattr(
        replay_resolver,
        "resolve_live_window",
        lambda source: (200, (300, 400, 800, 900)),
    )

    resolved = replay_resolver.resolve_replay_args(
        "computer_mouse",
        {"action": "down", "x": None, "y": None, "button": "left"},
        window_ref={"window_id": 100, "origin_x": 100, "origin_y": 100},
    )

    assert resolved == {"action": "down", "x": None, "y": None, "button": "left"}


def test_resolve_replay_manage_window_rewrites_stale_hwnd(monkeypatch):
    monkeypatch.setattr(
        replay_resolver,
        "resolve_live_window",
        lambda source: (222, (0, 0, 100, 100)),
    )

    resolved = replay_resolver.resolve_replay_args(
        "computer_manage_window",
        {"action": "activate", "window_id": "111", "window_title": "title", "process_name": "app.exe"},
    )

    assert resolved == {"action": "activate", "window_id": "222"}


def test_resolve_replay_click_relocates_cached_element(monkeypatch):
    cache = get_element_cache()
    cache.clear()
    monkeypatch.setattr(cache, "_is_window_valid", lambda _window_id: True)
    try:
        cache.set_window_origin(11, 22, 100, 200)
        cache.update(
            11,
            22,
            [
                CachedElement(
                    element_index=7,
                    source="uia",
                    center_x=10,
                    center_y=20,
                    bbox=(1, 2, 19, 38),
                    control_type="Button",
                    name="保存",
                    actions=["invoke"],
                )
            ],
        )

        resolved = replay_resolver.resolve_replay_args(
            "computer_click",
            {"x": 300, "y": 400, "button": "left", "pid": 11, "window_id": 22},
            element_ref={
                "recorded_element_index": 3,
                "source": "uia",
                "control_type": "Button",
                "name": "保存",
                "actions": ["invoke"],
                "center_x": 12,
                "center_y": 19,
            },
        )

        assert resolved == {"button": "left", "pid": 11, "window_id": 22, "element_index": 7}
    finally:
        cache.clear()


def test_resolve_replay_element_keeps_coordinate_fallback_when_no_match(monkeypatch):
    cache = get_element_cache()
    cache.clear()
    monkeypatch.setattr(cache, "_is_window_valid", lambda _window_id: True)
    try:
        args = {"x": 300, "y": 400, "button": "left", "pid": 11, "window_id": 22}
        resolved = replay_resolver.resolve_replay_args(
            "computer_click",
            args,
            element_ref={
                "recorded_element_index": 3,
                "control_type": "Button",
                "name": "保存",
            },
        )

        assert resolved == args
    finally:
        cache.clear()
