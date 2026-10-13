from __future__ import annotations

import pytest

from computer_server.handlers.element_cache import CachedElement, get_element_cache
from computer_server.services import replay_resolver


def test_resolve_replay_click_adjusts_coordinates_for_moved_window(monkeypatch):
    import computer_server.services.window_identity_service as window_identity

    monkeypatch.setattr(
        replay_resolver,
        "resolve_live_window",
        lambda source: (200, (300, 400, 800, 900)),
    )
    monkeypatch.setattr(
        window_identity,
        "build_window",
        lambda window_id: {"id": int(window_id), "pid": 999},
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

    assert resolved == {
        "x": 320,
        "y": 430,
        "window": {"id": 200, "pid": 999},
        "coordinate_space": "screen",
    }
    assert args["x"] == 120


def test_resolve_replay_drag_adjusts_both_points(monkeypatch):
    import computer_server.services.window_identity_service as window_identity

    monkeypatch.setattr(
        replay_resolver,
        "resolve_live_window",
        lambda source: (100, (50, 60, 500, 600)),
    )
    monkeypatch.setattr(
        window_identity,
        "build_window",
        lambda window_id: {"id": int(window_id), "pid": 999},
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
        "window": {"id": 100, "pid": 999},
        "coordinate_space": "screen",
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
    import computer_server.services.window_identity_service as window_identity

    monkeypatch.setattr(
        replay_resolver,
        "resolve_live_window",
        lambda source: (222, (0, 0, 100, 100)),
    )
    monkeypatch.setattr(
        window_identity,
        "build_window",
        lambda window_id: {"id": int(window_id)},
    )

    resolved = replay_resolver.resolve_replay_args(
        "computer_manage_window",
        {
            "action": "activate",
            "window": {"id": 111, "pid": 123},
            "window_title": "title",
            "process_name": "app.exe",
        },
    )

    assert resolved == {"action": "activate", "window": {"id": 222}}


def test_resolve_replay_keyboard_action_rebinds_target_window(monkeypatch):
    import computer_server.services.window_identity_service as window_identity

    monkeypatch.setattr(
        replay_resolver,
        "resolve_live_window",
        lambda source: (222, (0, 0, 100, 100)),
    )
    monkeypatch.setattr(
        window_identity,
        "build_window",
        lambda window_id: {"id": window_id, "pid": 999},
    )

    resolved = replay_resolver.resolve_replay_args(
        "computer_press_key",
        {
            "key": "Return",
            "window": {"id": 111, "pid": 123},
        },
        window_ref={"window_id": 111, "process_name": "app.exe"},
    )

    assert resolved == {
        "key": "Return",
        "window": {"id": 222, "pid": 999},
    }


@pytest.mark.parametrize("tool_name", sorted(replay_resolver.REPLAY_ELEMENT_TOOLS))
@pytest.mark.parametrize("recorded_name", [None, "send"])
def test_replay_rejects_vision_icon_when_old_position_is_reused(
    monkeypatch, tool_name, recorded_name,
):
    # The intended send icon moved to x=200; a different icon occupies x=100.
    elements = [
        CachedElement(
            element_index=index, source="vision", control_type="icon",
            name=None, actions=["click"], center_x=x, center_y=100,
            bbox=(x - 10, 90, x + 10, 110),
        )
        for index, x in [(10000, 100), (10001, 200)]
    ]
    monkeypatch.setattr(get_element_cache(), "get_elements", lambda *_: elements)
    with pytest.raises(ValueError, match="Cannot safely replay a vision-only icon"):
        replay_resolver.resolve_replay_args(
            tool_name,
            {
                "window": {"id": 22, "pid": 11},
                "element_index": 10042,
                "x": 100, "y": 100,
            },
            element_ref={
                "source": "vision", "control_type": "icon",
                "name": recorded_name, "actions": ["click"],
                "center_x": 100, "center_y": 100,
            },
        )


@pytest.mark.parametrize(
    ("source", "control_type"), [("uia", "Button"), ("vision", "text")],
)
def test_resolve_replay_click_relocates_cached_element(monkeypatch, source, control_type):
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
                    source=source,
                    center_x=10,
                    center_y=20,
                    bbox=(1, 2, 19, 38),
                    control_type=control_type,
                    name="保存",
                    actions=["invoke"],
                )
            ],
        )

        resolved = replay_resolver.resolve_replay_args(
            "computer_click",
            {
                "x": 300,
                "y": 400,
                "button": "left",
                "window": {"id": 22, "pid": 11},
            },
            element_ref={
                "recorded_element_index": 3,
                "source": source,
                "control_type": control_type,
                "name": "保存",
                "actions": ["invoke"],
                "center_x": 12,
                "center_y": 19,
            },
        )

        assert resolved == {
            "button": "left",
            "window": {"id": 22, "pid": 11},
            "element_index": 7,
        }
    finally:
        cache.clear()


def test_resolve_replay_secondary_action_relocates_cached_element(monkeypatch):
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
                    source="msaa",
                    center_x=10,
                    center_y=20,
                    bbox=(1, 2, 19, 38),
                    control_type="ButtonDropDown",
                    name="字体颜色",
                    actions=["invoke", "expand"],
                    metadata={"msaa_role_id": 0x38},
                )
            ],
        )

        resolved = replay_resolver.resolve_replay_args(
            "computer_perform_secondary_action",
            {
                "element_index": 3,
                "action": "expand",
                "window": {"id": 22, "pid": 11},
            },
            element_ref={
                "recorded_element_index": 3,
                "source": "msaa",
                "control_type": "ButtonDropDown",
                "name": "字体颜色",
                "actions": ["invoke", "expand"],
                "center_x": 12,
                "center_y": 19,
            },
        )

        assert resolved == {
            "element_index": 7,
            "action": "expand",
            "window": {"id": 22, "pid": 11},
        }
    finally:
        cache.clear()


def test_resolve_replay_element_keeps_coordinate_fallback_when_no_match(monkeypatch):
    cache = get_element_cache()
    cache.clear()
    monkeypatch.setattr(cache, "_is_window_valid", lambda _window_id: True)
    try:
        args = {
            "x": 300,
            "y": 400,
            "button": "left",
            "window": {"id": 22, "pid": 11},
        }
        resolved = replay_resolver.resolve_replay_args(
            "computer_click",
            args,
            element_ref={
                "recorded_element_index": 3,
                "control_type": "Button",
                "name": "保存",
            },
        )

        assert resolved == {**args, "coordinate_space": "screen"}
    finally:
        cache.clear()
