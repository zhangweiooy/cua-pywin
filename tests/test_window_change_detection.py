from __future__ import annotations


def test_window_change_detection_uses_win32process_for_process_ids(monkeypatch):
    import win32gui
    import win32process

    import computer_server.mcp_server as mcp_module

    def enum_windows(callback, context):
        callback(100, context)
        callback(200, context)

    monkeypatch.setattr(win32gui, "EnumWindows", enum_windows)
    monkeypatch.setattr(win32gui, "IsWindowVisible", lambda _hwnd: True)
    monkeypatch.setattr(win32gui, "GetForegroundWindow", lambda: 200)
    monkeypatch.setattr(
        win32gui,
        "GetWindowText",
        lambda hwnd: {100: "Original", 200: "Opened"}[hwnd],
    )
    monkeypatch.setattr(
        win32process,
        "GetWindowThreadProcessId",
        lambda hwnd: (10, {100: 1000, 200: 2000}[hwnd]),
    )

    changes = mcp_module._window_detect_changes(frozenset({100}), 100)

    assert changes == {
        "new_windows": [
            {
                "hwnd": 200,
                "pid": 2000,
                "title": "Opened",
                "cross_app": True,
            }
        ],
        "focus_stolen": {
            "from_hwnd": 100,
            "to_hwnd": 200,
            "to_pid": 2000,
            "to_title": "Opened",
        },
    }


def test_window_change_detection_failure_does_not_escape_after_invoke(monkeypatch):
    import win32process

    import computer_server.mcp_server as mcp_module

    monkeypatch.setattr(mcp_module, "_window_snapshot", lambda: (100, frozenset({100})))

    def unavailable_process_id(_hwnd):
        raise RuntimeError("window disappeared")

    monkeypatch.setattr(
        win32process,
        "GetWindowThreadProcessId",
        unavailable_process_id,
    )

    assert mcp_module._window_detect_changes(frozenset({100}), 100) is None
