from __future__ import annotations

import pytest

from computer_server.services.validation_service import ValidationError
from computer_server.services.window_identity_service import (
    add_window_field,
    build_window,
    build_window_ref,
    normalize_window_input,
    rehydrate_window,
    resolve_window_input,
)


def test_normalize_window_accepts_id_and_window_id():
    assert normalize_window_input({"id": "123", "window_id": 123, "pid": "456"}) == (
        456,
        123,
    )


def test_normalize_window_rejects_id_mismatch():
    with pytest.raises(ValidationError, match="window.id/window.window_id mismatch"):
        normalize_window_input({"id": 123, "window_id": 124})


def test_resolve_window_input_accepts_top_level_values():
    resolved = resolve_window_input(pid=456, window_id=123)

    assert resolved.pid == 456
    assert resolved.window_id == 123
    assert resolved.window is not None
    assert resolved.window["id"] == 123
    assert resolved.window["window_id"] == 123
    assert resolved.window["pid"] == 456


def test_resolve_window_input_infers_pid_from_window_id(monkeypatch):
    import computer_server.services.window_identity_service as identity

    monkeypatch.setattr(identity, "_get_pid_for_window", lambda _window_id: 456)

    resolved = resolve_window_input(window={"id": 123})

    assert resolved.pid == 456
    assert resolved.window_id == 123
    assert resolved.window is not None
    assert resolved.window["id"] == 123
    assert resolved.window["window_id"] == 123
    assert resolved.window["pid"] == 456


def test_resolve_window_input_rejects_pid_mismatch():
    with pytest.raises(ValidationError, match="window.pid mismatch"):
        resolve_window_input(window={"id": 123, "pid": 999}, pid=456, window_id=123)


def test_resolve_window_input_rejects_window_id_mismatch():
    with pytest.raises(ValidationError, match="window.window_id mismatch"):
        resolve_window_input(window={"id": 123, "pid": 456}, pid=456, window_id=124)


def test_build_window_keeps_public_ids():
    window = build_window(123, pid=456, title="Target", app="Target.exe")

    assert window is not None
    assert {
        key: window[key]
        for key in ("id", "window_id", "pid", "app", "process_name", "title")
    } == {
        "id": 123,
        "window_id": 123,
        "pid": 456,
        "app": "Target.exe",
        "process_name": "Target.exe",
        "title": "Target",
    }


def test_add_window_field_preserves_existing_record():
    record = {"id": 123, "window_id": 123, "pid": 456, "title": "Target"}

    assert add_window_field(record) is record
    assert record["id"] == 123
    assert record["window_id"] == 123
    assert record["window"]["id"] == 123
    assert record["window"]["window_id"] == 123
    assert record["window"]["pid"] == 456
    assert record["window"]["title"] == "Target"


def test_add_window_field_mirrors_canonical_window_fields(monkeypatch):
    import computer_server.services.window_identity_service as identity

    monkeypatch.setattr(identity, "_get_process_name", lambda _pid: "Weixin.exe")
    monkeypatch.setattr(identity, "_get_window_title", lambda _window_id: "微信")
    monkeypatch.setattr(identity, "_get_window_class", lambda _window_id: "Qt51514QWindowIcon")
    monkeypatch.setattr(identity, "_get_window_rect", lambda _window_id: (10, 20, 110, 220))
    monkeypatch.setattr(identity, "_is_window_visible", lambda _window_id: True)
    monkeypatch.setattr(identity, "_foreground_window_id", lambda: 999)

    record = {"id": 123, "window_id": 123, "pid": 456}

    add_window_field(record)

    assert record["app"] == "Weixin.exe"
    assert record["process_name"] == "Weixin.exe"
    assert record["title"] == "微信"
    assert record["class_name"] == "Qt51514QWindowIcon"
    assert record["bounds"] == {"x": 10, "y": 20, "width": 100, "height": 200}
    assert record["state"] == {
        "visible": True,
        "minimized": False,
        "foreground": False,
    }
    assert record["window"]["process_name"] == record["process_name"]
    assert record["window"]["class_name"] == record["class_name"]


def test_add_window_field_does_not_overwrite_existing_values(monkeypatch):
    import computer_server.services.window_identity_service as identity

    monkeypatch.setattr(identity, "_get_process_name", lambda _pid: "Actual.exe")
    monkeypatch.setattr(identity, "_get_window_class", lambda _window_id: "ActualClass")

    record = {
        "id": 123,
        "window_id": 123,
        "pid": 456,
        "app": "Hint.exe",
        "process_name": "Hint.exe",
        "class_name": "HintClass",
    }

    add_window_field(record)

    assert record["app"] == "Hint.exe"
    assert record["process_name"] == "Hint.exe"
    assert record["class_name"] == "HintClass"
    assert record["window"]["app"] == "Hint.exe"


def test_rehydrate_window_returns_canonical_window(monkeypatch):
    import computer_server.services.window_identity_service as identity

    monkeypatch.setattr(identity, "is_live_window", lambda _window_id: True)
    monkeypatch.setattr(identity, "_get_pid_for_window", lambda _window_id: 456)
    monkeypatch.setattr(identity, "_get_process_name", lambda _pid: "Target.exe")
    monkeypatch.setattr(identity, "_get_window_title", lambda _window_id: "Target")
    monkeypatch.setattr(identity, "_get_window_class", lambda _window_id: "TargetClass")
    monkeypatch.setattr(identity, "_get_window_rect", lambda _window_id: (10, 20, 110, 220))
    monkeypatch.setattr(identity, "_is_window_visible", lambda _window_id: True)
    monkeypatch.setattr(identity, "_foreground_window_id", lambda: 123)

    result = rehydrate_window(123, app="Display Name")

    assert result["success"] is True
    assert result["id"] == 123
    assert result["window_id"] == 123
    assert result["pid"] == 456
    assert result["title"] == "Target"
    assert result["app"] == "Target.exe"
    assert result["window"]["class_name"] == "TargetClass"
    assert result["window"]["bounds"] == {
        "x": 10,
        "y": 20,
        "width": 100,
        "height": 200,
    }
    assert result["window"]["state"] == {
        "visible": True,
        "minimized": False,
        "foreground": True,
    }


def test_rehydrate_window_rejects_non_integer_id():
    with pytest.raises(ValidationError, match="id must be an integer"):
        rehydrate_window("abc")


def test_rehydrate_window_returns_error_for_dead_hwnd(monkeypatch):
    import computer_server.services.window_identity_service as identity

    monkeypatch.setattr(identity, "is_live_window", lambda _window_id: False)

    assert rehydrate_window(123) == {
        "success": False,
        "error": "Window 123 is not currently available",
        "suggestion": "Call computer_list_apps or computer_list_windows to select a current window.",
    }


def test_rehydrate_window_allows_app_hint_mismatch(monkeypatch):
    import computer_server.services.window_identity_service as identity

    monkeypatch.setattr(identity, "is_live_window", lambda _window_id: True)
    monkeypatch.setattr(identity, "_get_pid_for_window", lambda _window_id: 456)
    monkeypatch.setattr(identity, "_get_process_name", lambda _pid: "Actual.exe")

    result = rehydrate_window(123, app="Different Display Name")

    assert result["success"] is True
    assert result["window"]["app"] == "Actual.exe"


def test_build_window_ref_includes_version_and_pid(monkeypatch):
    from computer_server.handlers.element_cache import get_element_cache

    cache = get_element_cache()
    cache.clear()
    monkeypatch.setattr(cache, "_is_window_valid", lambda _window_id: True)
    cache.set_window_origin(456, 123, 10, 20)

    ref = build_window_ref(456, 123, 0, 0)

    assert ref is not None
    assert ref["version"] == 1
    assert ref["pid"] == 456
    assert ref["window_id"] == 123
    assert ref["origin_x"] == 10
    assert ref["origin_y"] == 20
