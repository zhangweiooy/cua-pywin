from __future__ import annotations

import pytest

from computer_server.services.validation_service import ValidationError
from computer_server.services.window_identity_service import (
    add_window_field,
    build_window,
    build_window_ref,
    filter_window_records,
    find_best_window_record,
    is_window_minimized,
    normalize_window_input,
    rehydrate_window,
    resolve_window_input,
)


def test_normalize_window_accepts_canonical_id_and_pid():
    assert normalize_window_input({"id": "123", "pid": "456"}) == (
        456,
        123,
    )


def test_normalize_window_rejects_legacy_alias():
    with pytest.raises(ValidationError, match="only accepts canonical fields"):
        normalize_window_input({"id": 123, "window_id": 124})


def test_resolve_window_input_accepts_top_level_values():
    resolved = resolve_window_input(pid=456, window_id=123)

    assert resolved.pid == 456
    assert resolved.window_id == 123
    assert resolved.window is not None
    assert resolved.window == {"id": 123, "pid": 456}


def test_resolve_window_input_infers_pid_from_window_id(monkeypatch):
    import computer_server.services.window_identity_service as identity

    monkeypatch.setattr(identity, "_get_pid_for_window", lambda _window_id: 456)

    resolved = resolve_window_input(window={"id": 123})

    assert resolved.pid == 456
    assert resolved.window_id == 123
    assert resolved.window is not None
    assert resolved.window == {"id": 123, "pid": 456}


def test_resolve_window_input_rejects_pid_mismatch():
    with pytest.raises(ValidationError, match="window.pid mismatch"):
        resolve_window_input(window={"id": 123, "pid": 999}, pid=456, window_id=123)


def test_resolve_window_input_rejects_window_id_mismatch():
    with pytest.raises(ValidationError, match="window.window_id mismatch"):
        resolve_window_input(window={"id": 123, "pid": 456}, pid=456, window_id=124)


def test_resolve_window_input_rejects_live_owner_pid_mismatch(monkeypatch):
    import computer_server.services.window_identity_service as identity

    monkeypatch.setattr(identity, "_get_pid_for_window", lambda _window_id: 999)

    with pytest.raises(ValidationError, match="belongs to pid=999"):
        resolve_window_input(window={"id": 123, "pid": 456})


def test_get_pid_for_window_checks_hwnd_is_live(monkeypatch):
    import computer_server.services.window_identity_service as identity

    class Win32Gui:
        @staticmethod
        def IsWindow(_window_id):
            return False

    class Win32Process:
        @staticmethod
        def GetWindowThreadProcessId(_window_id):
            raise AssertionError("dead HWND must not reach owner lookup")

    modules = __import__("sys").modules
    monkeypatch.setitem(modules, "win32gui", Win32Gui)
    monkeypatch.setitem(modules, "win32process", Win32Process)

    assert identity._get_pid_for_window(123) is None


def test_build_window_returns_only_canonical_ids():
    window = build_window(123, pid=456, title="Target", app="Target.exe")

    assert window == {"id": 123, "pid": 456}


def test_is_window_minimized_uses_inclusive_iconic_coordinate_fallback(
    monkeypatch,
):
    import computer_server.services.window_identity_service as identity

    monkeypatch.setattr(
        identity,
        "_get_window_rect",
        lambda _window_id: (-32000, -32000, -31686, -31950),
    )
    monkeypatch.setitem(__import__("sys").modules, "win32gui", None)

    assert is_window_minimized(123) is True


def test_build_window_does_not_embed_mutable_window_state():
    window = build_window(123, pid=456, title="Target", app="Target.exe")

    assert window == {"id": 123, "pid": 456}


def test_add_window_field_preserves_existing_record():
    record = {"id": 123, "window_id": 123, "pid": 456, "title": "Target"}

    assert add_window_field(record) is record
    assert record["id"] == 123
    assert record["window_id"] == 123
    assert record["window"] == {"id": 123, "pid": 456}


def test_add_window_field_does_not_mirror_metadata_into_window():
    record = {"id": 123, "window_id": 123, "pid": 456}

    add_window_field(record)

    assert record == {
        "id": 123,
        "window_id": 123,
        "pid": 456,
        "window": {"id": 123, "pid": 456},
    }


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
    assert record["window"] == {"id": 123, "pid": 456}


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
    assert result["title"] == "Target"
    assert result["app"] == "Display Name"
    assert result["window"] == {"id": 123, "pid": 456}


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
    assert result["app"] == "Different Display Name"
    assert result["window"] == {"id": 123, "pid": 456}


def test_filter_window_records_matches_chinese_app_aliases():
    records = [
        {
            "id": 123,
            "window_id": 123,
            "pid": 456,
            "title": "",
            "app": "Weixin.exe",
            "process_name": "Weixin.exe",
            "class_name": "Qt51514QWindowIcon",
        },
        {
            "id": 124,
            "window_id": 124,
            "pid": 457,
            "title": "",
            "app": "Other.exe",
            "process_name": "Other.exe",
        },
    ]

    assert filter_window_records(records, query="微信") == [records[0]]
    assert filter_window_records(records, app="微信") == [records[0]]


def test_filter_window_records_does_not_reverse_substring_match():
    records = [
        {
            "id": 123,
            "window_id": 123,
            "pid": 456,
            "title": "Editor",
            "app": "Code.exe",
            "process_name": "Code.exe",
        }
    ]

    assert filter_window_records(records, query="Codex") == []


def test_find_best_window_record_uses_shared_alias_scoring(monkeypatch):
    import computer_server.services.window_identity_service as identity

    records = [
        {
            "id": 123,
            "window_id": 123,
            "pid": 456,
            "title": "",
            "app": "Weixin.exe",
            "process_name": "Weixin.exe",
            "width": 100,
            "height": 100,
        },
        {
            "id": 124,
            "window_id": 124,
            "pid": 457,
            "title": "微信",
            "app": "Helper.exe",
            "process_name": "Helper.exe",
            "width": 200,
            "height": 100,
        },
    ]
    monkeypatch.setattr(identity, "enumerate_window_records", lambda: records)

    match = find_best_window_record(app_hint="微信")

    assert match is records[0]


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
