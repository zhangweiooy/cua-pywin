from __future__ import annotations

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


def test_resolve_replay_activate_window_rewrites_stale_hwnd(monkeypatch):
    monkeypatch.setattr(
        replay_resolver,
        "resolve_live_window",
        lambda source: (222, (0, 0, 100, 100)),
    )

    resolved = replay_resolver.resolve_replay_args(
        "computer_activate_window",
        {"window_id": "111", "window_title": "title", "process_name": "app.exe"},
    )

    assert resolved == {"window_id": "222"}
