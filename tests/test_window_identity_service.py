from __future__ import annotations

import pytest

from computer_server.services.validation_service import ValidationError
from computer_server.services.window_identity_service import (
    add_window_field,
    build_window,
    build_window_ref,
    normalize_window_input,
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


def test_resolve_window_input_accepts_legacy_values():
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


def test_build_window_keeps_legacy_ids():
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


def test_add_window_field_preserves_legacy_record():
    record = {"id": 123, "window_id": 123, "pid": 456, "title": "Target"}

    assert add_window_field(record) is record
    assert record["id"] == 123
    assert record["window_id"] == 123
    assert record["window"]["id"] == 123
    assert record["window"]["window_id"] == 123
    assert record["window"]["pid"] == 456
    assert record["window"]["title"] == "Target"


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
