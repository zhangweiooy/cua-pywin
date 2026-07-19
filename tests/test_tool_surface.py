from __future__ import annotations

import os
from types import SimpleNamespace

os.environ["CUA_SKIP_SOM_WARMUP"] = "1"

import pytest

from computer_server.handlers.element_cache import CachedElement, get_element_cache
from computer_server.mcp_server import create_mcp_server


class _FakeAutomation:
    def __init__(self):
        self.mouse = SimpleNamespace(position=(0, 0), scroll=lambda _x, _y: None)
        self.moves = []
        self.mouse_events = []
        self.key_events = []
        self.typed_text = []

    async def move_cursor(self, x: int, y: int):
        self.moves.append((x, y))
        return {"success": True, "x": x, "y": y}

    async def left_click(self, x: int, y: int):
        return {"success": True, "x": x, "y": y}

    async def right_click(self, x: int, y: int):
        return {"success": True, "x": x, "y": y}

    async def middle_click(self, x: int, y: int):
        return {"success": True, "x": x, "y": y}

    async def mouse_down(self, x: int, y: int, button: str = "left"):
        self.mouse_events.append(("down", x, y, button))
        return {"success": True, "x": x, "y": y, "button": button}

    async def mouse_up(self, x: int, y: int, button: str = "left"):
        self.mouse_events.append(("up", x, y, button))
        return {"success": True, "x": x, "y": y, "button": button}

    def _map_button(self, button: str):
        return button

    async def scroll(self, scroll_x: int, scroll_y: int):
        return {"success": True, "scroll_x": scroll_x, "scroll_y": scroll_y}

    async def hotkey(self, keys):
        self.key_events.append(("hotkey", list(keys)))
        return {"success": True, "keys": keys}

    async def press_key(self, key):
        self.key_events.append(("press", key))
        return {"success": True, "key": key}

    async def key_down(self, key):
        self.key_events.append(("down", key))
        return {"success": True, "key": key, "action": "down"}

    async def key_up(self, key):
        self.key_events.append(("up", key))
        return {"success": True, "key": key, "action": "up"}

    async def type_text(self, text: str):
        self.typed_text.append(text)
        return {"success": True, "text": text}


@pytest.mark.asyncio
async def test_screenshot_returns_image_and_structured_metadata(monkeypatch):
    import base64
    from io import BytesIO

    from PIL import Image as PILImage

    import computer_server.mcp_server as mcp_module

    image = PILImage.new("RGB", (32, 24), "white")
    buffer = BytesIO()
    image.save(buffer, format="PNG")

    class Automation(_FakeAutomation):
        async def screenshot(self, format: str, quality: int):
            assert format == "png"
            assert quality == 85
            return {
                "success": True,
                "image_data": base64.b64encode(buffer.getvalue()).decode("ascii"),
            }

    monkeypatch.setattr(
        mcp_module,
        "_handlers",
        SimpleNamespace(automation=Automation()),
    )

    result = await create_mcp_server().call_tool(
        "computer_screenshot",
        {"format": "png"},
    )

    assert len(result.content) == 1
    assert result.content[0].type == "image"
    assert result.content[0].mimeType == "image/png"
    assert result.structured_content == {
        "success": True,
        "format": "png",
        "mime_type": "image/png",
        "mimeType": "image/png",
        "width": 32,
        "height": 24,
        "byte_count": len(base64.b64decode(result.content[0].data)),
        "imageBytes": len(base64.b64decode(result.content[0].data)),
    }


class _FakeWindow:
    def __init__(self):
        self.activations = []

    async def get_current_window_id(self):
        return {"success": True, "window_id": 456, "name": "Target Window"}

    async def activate_window(self, window_id: int):
        self.activations.append(window_id)
        return {"success": True, "window_id": window_id}

    async def minimize_window(self, window_id: int):
        return {"success": True, "window_id": window_id}

    async def maximize_window(self, window_id: int):
        return {"success": True, "window_id": window_id}

    async def close_window(self, window_id: int):
        return {"success": True, "window_id": window_id}

    async def set_window_size(self, window_id: int, width: int, height: int):
        return {"success": True, "window_id": window_id, "width": width, "height": height}

    async def set_window_position(self, window_id: int, x: int, y: int):
        return {"success": True, "window_id": window_id, "x": x, "y": y}

    async def get_application_windows(self, app: str):
        assert app == "Target"
        return {
            "success": True,
            "windows": [
                {"id": 456, "window_id": 456, "title": "Target Window"},
            ],
        }


@pytest.mark.asyncio
async def test_launch_app_can_wait_for_canonical_window(monkeypatch):
    import computer_server.mcp_server as mcp_module
    import computer_server.services.window_identity_service as identity_module

    class Window(_FakeWindow):
        async def launch(self, app, args=None):
            return {
                "success": True,
                "pid": 111,
                "resolved_path": r"C:\Program Files\Weixin\Weixin.exe",
            }

    calls = []

    def find_app_window(**kwargs):
        calls.append(kwargs)
        return {
            "id": 456,
            "window_id": 456,
            "pid": 222,
            "app": "Weixin.exe",
            "process_name": "Weixin.exe",
            "title": "微信",
        }

    monkeypatch.setattr(
        mcp_module,
        "_handlers",
        SimpleNamespace(window=Window()),
    )
    monkeypatch.setattr(identity_module, "find_app_window", find_app_window)

    result = await create_mcp_server().call_tool(
        "computer_launch_app",
        {"app": "微信", "wait_for_window": True, "wait_timeout": 1},
    )

    content = result.structured_content
    assert content["success"] is True
    assert content["window_ready"] is True
    assert content["launch_pid"] == 111
    assert content["pid"] == 222
    assert content["window"]["id"] == 456
    assert calls[0]["process_name"] == "Weixin.exe"
    assert calls[0]["app_hint"] == "微信"


@pytest.mark.asyncio
async def test_launch_app_window_wait_timeout_is_explicit(monkeypatch):
    import computer_server.mcp_server as mcp_module
    import computer_server.services.window_identity_service as identity_module

    class Window(_FakeWindow):
        async def launch(self, app, args=None):
            return {"success": True, "pid": 111, "resolved_path": "sample.exe"}

    monkeypatch.setattr(
        mcp_module,
        "_handlers",
        SimpleNamespace(window=Window()),
    )
    monkeypatch.setattr(identity_module, "find_app_window", lambda **kwargs: None)

    result = await create_mcp_server().call_tool(
        "computer_launch_app",
        {"app": "sample", "wait_for_window": True, "wait_timeout": 0.1},
    )

    content = result.structured_content
    assert content["success"] is True
    assert content["window_ready"] is False
    assert "no targetable window appeared" in content["warning"]


@pytest.mark.asyncio
async def test_launch_app_falls_back_to_running_alias_window(monkeypatch):
    import computer_server.mcp_server as mcp_module
    import computer_server.services.window_identity_service as identity_module

    class Window(_FakeWindow):
        async def launch(self, app, args=None):
            return {"success": False, "error": "Could not locate app"}

    window = Window()

    def find_app_window(**kwargs):
        assert kwargs["app_hint"] == "微信"
        assert kwargs["title_hint"] == "微信"
        return {
            "id": 456,
            "window_id": 456,
            "pid": 222,
            "app": "Weixin.exe",
            "process_name": "Weixin.exe",
            "title": "微信",
        }

    monkeypatch.setattr(
        mcp_module,
        "_handlers",
        SimpleNamespace(window=window),
    )
    monkeypatch.setattr(identity_module, "find_app_window", find_app_window)

    result = await create_mcp_server().call_tool(
        "computer_launch_app",
        {"app": "微信", "wait_for_window": True, "wait_timeout": 1},
    )

    content = result.structured_content
    assert content["success"] is True
    assert content["already_running"] is True
    assert content["window_ready"] is True
    assert content["window_id"] == 456
    assert content["pid"] == 222
    assert content["window"]["process_name"] == "Weixin.exe"
    assert content["activation"]["success"] is True
    assert window.activations == [456]
    assert content["launch_fallback"]["launch_error"] == "Could not locate app"


EXPECTED_PUBLIC_TOOLS = {
    "computer_screenshot",
    "computer_get_screen_size",
    "computer_get_cursor_position",
    "computer_click",
    "computer_perform_secondary_action",
    "computer_mouse",
    "computer_drag",
    "computer_scroll",
    "computer_type",
    "computer_press_key",
    "computer_release_all_inputs",
    "computer_set_value",
    "computer_open_url",
    "computer_launch_app",
    "computer_get_window",
    "computer_manage_window",
    "computer_list_apps",
    "computer_list_windows",
    "computer_get_desktop_environment",
    "computer_get_window_state",
    "computer_zoom",
    "computer_page",
    "computer_recording",
    "computer_replay_trajectory",
    "computer_learn",
}


@pytest.mark.asyncio
async def test_agent_native_tools_are_not_exposed():
    mcp = create_mcp_server()
    tools = await mcp.list_tools()
    names = {tool.name for tool in tools}

    assert names == EXPECTED_PUBLIC_TOOLS
    click = next(tool for tool in tools if tool.name == "computer_click")
    secondary = next(
        tool for tool in tools if tool.name == "computer_perform_secondary_action"
    )
    assert "action" not in click.parameters["properties"]
    assert set(secondary.parameters["required"]) == {"element_index", "action"}


@pytest.mark.asyncio
async def test_open_url_rejects_local_paths():
    result = await create_mcp_server().call_tool(
        "computer_open_url",
        {"url": "C:/Windows"},
    )

    assert result.structured_content == {
        "success": False,
        "error": "url must be an absolute http or https URL",
    }


@pytest.mark.asyncio
async def test_click_requires_coordinates_or_element():
    result = await create_mcp_server().call_tool("computer_click", {})

    assert result.structured_content == {
        "success": False,
        "error": "x and y are required (or element_index + pid + window_id)",
        "suggestion": (
            "Provide (x, y) pixel coordinates or (element_index + pid + window_id) "
            "from a previous get_window_state call"
        ),
    }


@pytest.mark.asyncio
async def test_click_rejects_empty_verify_query_before_action():
    result = await create_mcp_server().call_tool(
        "computer_click",
        {"x": 1, "y": 1, "verify_query": "   "},
    )

    assert result.structured_content == {
        "success": False,
        "error": "verify_query must not be empty",
    }


@pytest.mark.asyncio
async def test_click_require_verification_requires_query():
    result = await create_mcp_server().call_tool(
        "computer_click",
        {"x": 1, "y": 1, "require_verification": True},
    )

    assert result.structured_content == {
        "success": False,
        "error": "require_verification=True requires verify_query",
    }


@pytest.mark.asyncio
async def test_click_returns_action_path_for_pixel_click(monkeypatch):
    import computer_server.mcp_server as mcp_module

    async def noop_animate(_x: int, _y: int) -> None:
        return None

    monkeypatch.setattr(
        mcp_module,
        "_handlers",
        SimpleNamespace(automation=_FakeAutomation()),
    )
    monkeypatch.setattr(mcp_module, "_animate_cursor_to", noop_animate)

    result = await create_mcp_server().call_tool(
        "computer_click",
        {"x": 1, "y": 2},
    )

    assert result.structured_content["success"] is True
    assert result.structured_content["method"] == "pixel_click"
    action_path = result.structured_content["action_path"]
    assert action_path == {
        "method": "pixel_click",
        "used_foreground": True,
        "uses_system_input": True,
        "requires_visible_target": True,
        "target_profile": {"family": "generic"},
        "delivery_risk": "visible_target_required",
        "delivery_note": (
            "Pixel/system input requires the target point to be visible and focused."
        ),
        "coordinate_source": "screen",
    }


@pytest.mark.asyncio
async def test_type_returns_target_profile_action_path(monkeypatch):
    import computer_server.mcp_server as mcp_module

    automation = _FakeAutomation()
    window_handler = _FakeWindow()
    monkeypatch.setattr(
        mcp_module,
        "_handlers",
        SimpleNamespace(automation=automation, window=window_handler),
    )

    result = await create_mcp_server().call_tool(
        "computer_type",
        {
            "text": "hello",
            "window": {
                "id": 456,
                "pid": 123,
                "process_name": "pwsh.exe",
                "class_name": "CASCADIA_HOSTING_WINDOW_CLASS",
                "title": "PowerShell",
            },
        },
    )

    action_path = result.structured_content["action_path"]
    assert action_path["method"] == "clipboard_type"
    assert action_path["target_profile"]["family"] == "terminal"
    assert action_path["delivery_risk"] == "terminal_focus_sensitive"
    assert automation.typed_text == ["hello"]
    assert window_handler.activations == [456]


@pytest.mark.asyncio
async def test_press_key_returns_target_profile_action_path(monkeypatch):
    import computer_server.mcp_server as mcp_module

    automation = _FakeAutomation()
    window_handler = _FakeWindow()
    monkeypatch.setattr(
        mcp_module,
        "_handlers",
        SimpleNamespace(automation=automation, window=window_handler),
    )

    result = await create_mcp_server().call_tool(
        "computer_press_key",
        {
            "key": "Control_L+s",
            "window": {
                "id": 456,
                "pid": 123,
                "process_name": "soffice.bin",
                "class_name": "SALFRAME",
                "title": "LibreOffice Writer",
            },
        },
    )

    action_path = result.structured_content["action_path"]
    assert action_path["method"] == "hotkey"
    assert action_path["target_profile"]["family"] == "vcl_sal"
    assert action_path["delivery_risk"] == "accelerator_state_sensitive"
    assert automation.key_events == [("hotkey", ["Control_L", "s"])]
    assert window_handler.activations == [456]


@pytest.mark.asyncio
async def test_secondary_action_requires_window_identity():
    result = await create_mcp_server().call_tool(
        "computer_perform_secondary_action",
        {"element_index": 1, "action": "expand"},
    )

    assert result.structured_content == {
        "success": False,
        "error": "pid and window_id are required with element_index",
        "suggestion": "Pass the window and snapshot_id from computer_get_window_state",
    }


@pytest.mark.asyncio
async def test_secondary_action_rejects_non_secondary_action():
    result = await create_mcp_server().call_tool(
        "computer_perform_secondary_action",
        {"element_index": 1, "action": "raise"},
    )

    assert result.structured_content == {
        "success": False,
        "error": "action must be one of: expand",
    }


@pytest.mark.asyncio
async def test_secondary_action_expands_msaa_dropdown(monkeypatch):
    import computer_server.mcp_server as mcp_module

    cache = get_element_cache()
    cache.clear()
    monkeypatch.setattr(cache, "_is_window_valid", lambda _window_id: True)

    async def noop_animate(_x: int, _y: int) -> None:
        return None

    automation = _FakeAutomation()
    window = _FakeWindow()
    monkeypatch.setattr(
        mcp_module,
        "_handlers",
        SimpleNamespace(automation=automation, window=window),
    )
    monkeypatch.setattr(mcp_module, "_animate_cursor_to", noop_animate)
    try:
        cache.set_window_origin(123, 456, 100, 200)
        cache.update(
            123,
            456,
            [
                CachedElement(
                    element_index=7,
                    source="msaa",
                    center_x=40,
                    center_y=20,
                    bbox=(20, 10, 60, 30),
                    control_type="ButtonDropDown",
                    name="字体颜色",
                    actions=["invoke", "expand"],
                    metadata={"msaa_role": "ButtonDropDown", "msaa_role_id": 0x38},
                )
            ],
        )

        result = await create_mcp_server().call_tool(
            "computer_perform_secondary_action",
            {
                "element_index": 7,
                "action": "expand",
                "pid": 123,
                "window_id": 456,
            },
        )

        content = result.structured_content
        assert content["success"] is True
        assert content["action"] == "expand"
        assert content["element_index"] == 7
        assert content["x"] == 156
        assert content["y"] == 220
        assert content["method"] == "msaa_expand_edge_click"
        assert content["action_path"]["coordinate_source"] == "element"
        assert window.activations == [456]
    finally:
        cache.clear()


@pytest.mark.asyncio
async def test_get_window_active_returns_canonical_window(monkeypatch):
    import computer_server.mcp_server as mcp_module

    monkeypatch.setattr(
        mcp_module,
        "_handlers",
        SimpleNamespace(window=_FakeWindow()),
    )

    result = await create_mcp_server().call_tool(
        "computer_get_window",
        {"active": True},
    )

    window_record = result.structured_content
    assert window_record["id"] == 456
    assert window_record["window_id"] == 456
    assert window_record["window"]["id"] == 456
    assert window_record["window"]["window_id"] == 456
    assert window_record["window"]["title"] == "Target Window"


@pytest.mark.asyncio
async def test_manage_window_activates_and_resizes(monkeypatch):
    import computer_server.mcp_server as mcp_module

    monkeypatch.setattr(
        mcp_module,
        "_handlers",
        SimpleNamespace(window=_FakeWindow()),
    )

    mcp = create_mcp_server()
    activate_result = await mcp.call_tool(
        "computer_manage_window",
        {"window": {"id": 456, "pid": 123}, "action": "activate"},
    )
    resize_result = await mcp.call_tool(
        "computer_manage_window",
        {"window_id": 456, "action": "resize", "width": 800, "height": 600},
    )

    assert activate_result.structured_content["success"] is True
    assert activate_result.structured_content["action"] == "activate"
    assert resize_result.structured_content["success"] is True
    assert resize_result.structured_content["window_id"] == 456
    assert resize_result.structured_content["width"] == 800
    assert resize_result.structured_content["height"] == 600
    assert resize_result.structured_content["action"] == "resize"


@pytest.mark.asyncio
async def test_list_windows_marks_minimized_window_off_screen(monkeypatch):
    import win32con
    import win32gui
    import win32process

    import computer_server.services.window_identity_service as identity

    monkeypatch.setattr(
        win32gui,
        "EnumWindows",
        lambda callback, extra: callback(456, extra),
    )
    monkeypatch.setattr(win32gui, "IsWindowVisible", lambda _hwnd: True)
    monkeypatch.setattr(
        win32gui,
        "GetWindowLong",
        lambda _hwnd, _index: win32con.WS_VISIBLE,
    )
    monkeypatch.setattr(win32gui, "GetWindowText", lambda _hwnd: "Target")
    monkeypatch.setattr(
        win32gui,
        "GetWindowRect",
        lambda _hwnd: (-32000, -32000, -31686, -31950),
    )
    monkeypatch.setattr(
        win32process,
        "GetWindowThreadProcessId",
        lambda _hwnd: (1, 123),
    )
    monkeypatch.setattr(identity, "_is_meaningful_window", lambda _hwnd: True)
    monkeypatch.setattr(
        identity,
        "is_window_minimized",
        lambda _hwnd, _rect=None: True,
    )

    mcp = create_mcp_server()
    all_windows = await mcp.call_tool("computer_list_windows", {})
    on_screen = await mcp.call_tool(
        "computer_list_windows",
        {"on_screen_only": True},
    )

    record = all_windows.structured_content["windows"][0]
    assert record["minimized"] is True
    assert record["is_on_screen"] is False
    assert record["window"]["state"]["minimized"] is True
    assert on_screen.structured_content["windows"] == []


@pytest.mark.asyncio
async def test_list_apps_uses_shared_window_records(monkeypatch):
    import computer_server.services.window_identity_service as identity

    monkeypatch.setattr(
        identity,
        "enumerate_window_records",
        lambda: [
            {
                "id": 456,
                "window_id": 456,
                "pid": 123,
                "title": "微信",
                "app": "Weixin.exe",
                "process_name": "Weixin.exe",
                "x": 0,
                "y": 0,
                "width": 800,
                "height": 600,
                "is_on_screen": True,
                "minimized": False,
            }
        ],
    )

    result = await create_mcp_server().call_tool("computer_list_apps", {})

    apps = result.structured_content["apps"]
    assert len(apps) == 1
    assert apps[0]["name"] == "Weixin.exe"
    assert apps[0]["windows"][0]["window"]["id"] == 456


@pytest.mark.asyncio
async def test_list_windows_query_matches_unicode_window_title(monkeypatch):
    import win32con
    import win32gui
    import win32process

    import computer_server.services.window_identity_service as identity

    monkeypatch.setattr(
        win32gui,
        "EnumWindows",
        lambda callback, extra: callback(456, extra),
    )
    monkeypatch.setattr(win32gui, "IsWindowVisible", lambda _hwnd: True)
    monkeypatch.setattr(
        win32gui,
        "GetWindowLong",
        lambda _hwnd, _index: win32con.WS_VISIBLE,
    )
    monkeypatch.setattr(win32gui, "GetWindowText", lambda _hwnd: "\u5fae\u4fe1")
    monkeypatch.setattr(win32gui, "GetWindowRect", lambda _hwnd: (0, 0, 800, 600))
    monkeypatch.setattr(
        win32process,
        "GetWindowThreadProcessId",
        lambda _hwnd: (1, 123),
    )
    monkeypatch.setattr(identity, "_is_meaningful_window", lambda _hwnd: True)
    monkeypatch.setattr(
        identity,
        "is_window_minimized",
        lambda _hwnd, _rect=None: False,
    )

    mcp = create_mcp_server()
    result = await mcp.call_tool(
        "computer_list_windows",
        {"query": "\u5fae\u4fe1"},
    )

    windows = result.structured_content["windows"]
    assert len(windows) == 1
    assert windows[0]["title"] == "\u5fae\u4fe1"


@pytest.mark.asyncio
async def test_mouse_accepts_move_and_button_actions(monkeypatch):
    import computer_server.mcp_server as mcp_module

    fake = _FakeAutomation()

    async def noop_animate(_x: int, _y: int) -> None:
        return None

    monkeypatch.setattr(
        mcp_module,
        "_handlers",
        SimpleNamespace(automation=fake),
    )
    monkeypatch.setattr(mcp_module, "_animate_cursor_to", noop_animate)

    mcp = create_mcp_server()
    move_result = await mcp.call_tool(
        "computer_mouse",
        {"action": "move", "x": 10, "y": 20},
    )
    down_result = await mcp.call_tool(
        "computer_mouse",
        {"action": "down", "x": 10, "y": 20, "button": "right"},
    )

    assert move_result.structured_content == {"success": True, "x": 10, "y": 20}
    assert down_result.structured_content == {
        "success": True,
        "x": 10,
        "y": 20,
        "button": "right",
    }
    assert fake.moves == [(10, 20)]
    assert fake.mouse_events == [("down", 10, 20, "right")]


@pytest.mark.asyncio
async def test_press_key_accepts_hotkey_string(monkeypatch):
    import computer_server.mcp_server as mcp_module

    monkeypatch.setattr(
        mcp_module,
        "_handlers",
        SimpleNamespace(automation=_FakeAutomation()),
    )

    result = await create_mcp_server().call_tool(
        "computer_press_key",
        {"key": "Control_L+s"},
    )

    content = result.structured_content
    assert content["success"] is True
    assert content["keys"] == ["Control_L", "s"]
    assert content["action_path"]["method"] == "hotkey"
    assert content["action_path"]["delivery_risk"] == "focused_input_required"


@pytest.mark.asyncio
async def test_type_activates_target_window_before_input(monkeypatch):
    import computer_server.mcp_server as mcp_module

    events = []

    class Automation(_FakeAutomation):
        async def type_text(self, text: str):
            events.append(("type", text))
            return {"success": True}

    class Window(_FakeWindow):
        async def activate_window(self, window_id: int):
            events.append(("activate", window_id))
            return {"success": True}

    monkeypatch.setattr(
        mcp_module,
        "_handlers",
        SimpleNamespace(automation=Automation(), window=Window()),
    )

    result = await create_mcp_server().call_tool(
        "computer_type",
        {
            "text": "你好",
            "window": {"id": 456, "pid": 123},
        },
    )

    assert result.structured_content["success"] is True
    assert result.structured_content["window_id"] == 456
    assert result.structured_content["pid"] == 123
    assert result.structured_content["target_window_activated"] is True
    assert events == [("activate", 456), ("type", "你好")]


@pytest.mark.asyncio
async def test_press_key_does_not_send_input_when_activation_fails(monkeypatch):
    import computer_server.mcp_server as mcp_module

    automation = _FakeAutomation()

    class Window(_FakeWindow):
        async def activate_window(self, window_id: int):
            return {"success": False, "error": "foreground denied"}

    monkeypatch.setattr(
        mcp_module,
        "_handlers",
        SimpleNamespace(automation=automation, window=Window()),
    )

    result = await create_mcp_server().call_tool(
        "computer_press_key",
        {
            "key": "Return",
            "window": {"id": 456, "pid": 123},
        },
    )

    assert result.structured_content["success"] is False
    assert "Failed to activate target window 456" in result.structured_content["error"]
    assert automation.key_events == []


@pytest.mark.asyncio
async def test_press_key_up_releases_without_reactivating_window(monkeypatch):
    import computer_server.mcp_server as mcp_module

    automation = _FakeAutomation()
    window = _FakeWindow()
    monkeypatch.setattr(
        mcp_module,
        "_handlers",
        SimpleNamespace(automation=automation, window=window),
    )

    result = await create_mcp_server().call_tool(
        "computer_press_key",
        {
            "key": "Shift",
            "action": "up",
            "window": {"id": 456, "pid": 123},
        },
    )

    assert result.structured_content["success"] is True
    assert result.structured_content["target_window_activated"] is False
    assert window.activations == []
    assert automation.key_events == [("up", "Shift")]


@pytest.mark.asyncio
async def test_press_key_accepts_down_and_up_actions(monkeypatch):
    import computer_server.mcp_server as mcp_module

    fake = _FakeAutomation()
    monkeypatch.setattr(
        mcp_module,
        "_handlers",
        SimpleNamespace(automation=fake),
    )

    mcp = create_mcp_server()
    down_result = await mcp.call_tool(
        "computer_press_key",
        {"key": "Shift", "action": "down"},
    )
    up_result = await mcp.call_tool(
        "computer_press_key",
        {"key": "Shift", "action": "up"},
    )

    assert down_result.structured_content["success"] is True
    assert down_result.structured_content["key"] == "Shift"
    assert down_result.structured_content["action"] == "down"
    assert down_result.structured_content["action_path"]["method"] == "key_down"
    assert up_result.structured_content["success"] is True
    assert up_result.structured_content["key"] == "Shift"
    assert up_result.structured_content["action"] == "up"
    assert up_result.structured_content["action_path"]["method"] == "key_up"
    assert fake.key_events == [("down", "Shift"), ("up", "Shift")]


@pytest.mark.asyncio
async def test_mouse_actions_activate_explicit_window_before_input(monkeypatch):
    import computer_server.mcp_server as mcp_module

    events = []

    class Automation(_FakeAutomation):
        def __init__(self):
            super().__init__()
            self.mouse = SimpleNamespace(
                position=(0, 0),
                scroll=lambda x, y: events.append(("wheel", x, y)),
            )

        async def left_click(self, x: int, y: int):
            events.append(("click", x, y))
            return {"success": True, "x": x, "y": y}

        async def move_cursor(self, x: int, y: int):
            events.append(("move", x, y))
            return {"success": True, "x": x, "y": y}

        async def mouse_down(self, x: int, y: int, button: str = "left"):
            events.append(("down", x, y, button))
            return {"success": True}

        async def mouse_up(self, x: int, y: int, button: str = "left"):
            events.append(("up", x, y, button))
            return {"success": True}

        async def scroll(self, scroll_x: int, scroll_y: int):
            events.append(("scroll", scroll_x, scroll_y))
            return {"success": True}

    class Window(_FakeWindow):
        async def activate_window(self, window_id: int):
            events.append(("activate", window_id))
            return {"success": True}

    async def noop_animate(_x: int, _y: int) -> None:
        return None

    monkeypatch.setattr(
        mcp_module,
        "_handlers",
        SimpleNamespace(automation=Automation(), window=Window()),
    )
    monkeypatch.setattr(mcp_module, "_animate_cursor_to", noop_animate)
    mcp = create_mcp_server()
    target = {"id": 456, "pid": 123}

    click = await mcp.call_tool(
        "computer_click",
        {
            "window": target,
            "coordinate_space": "screen",
            "x": 10,
            "y": 20,
        },
    )
    assert events[:2] == [("activate", 456), ("click", 10, 20)]
    assert click.structured_content["target_window_activated"] is True

    events.clear()
    drag = await mcp.call_tool(
        "computer_drag",
        {
            "window": target,
            "coordinate_space": "screen",
            "start_x": 10,
            "start_y": 20,
            "end_x": 30,
            "end_y": 40,
            "duration": 0.001,
        },
    )
    assert events[0] == ("activate", 456)
    assert ("down", 10, 20, "left") in events
    assert drag.structured_content["target_window_activated"] is True

    events.clear()
    scroll = await mcp.call_tool(
        "computer_scroll",
        {
            "window": target,
            "coordinate_space": "screen",
            "x": 10,
            "y": 20,
            "scroll_y": -1,
            "smooth": False,
        },
    )
    assert events == [
        ("activate", 456),
        ("move", 10, 20),
        ("scroll", 0, -1),
    ]
    assert scroll.structured_content["target_window_activated"] is True

    events.clear()
    smooth_scroll = await mcp.call_tool(
        "computer_scroll",
        {
            "window": target,
            "coordinate_space": "screen",
            "x": 10,
            "y": 20,
            "scroll_y": -2,
            "smooth": True,
        },
    )
    assert events == [
        ("activate", 456),
        ("move", 10, 20),
        ("wheel", 0, -1),
        ("wheel", 0, -1),
    ]
    assert smooth_scroll.structured_content["success"] is True
    assert smooth_scroll.structured_content["target_window_activated"] is True


@pytest.mark.asyncio
async def test_click_does_not_send_input_when_explicit_window_activation_fails(
    monkeypatch,
):
    import computer_server.mcp_server as mcp_module

    automation = _FakeAutomation()

    class Window(_FakeWindow):
        async def activate_window(self, window_id: int):
            return {"success": False, "error": "foreground denied"}

    async def fail_animate(_x: int, _y: int) -> None:
        raise AssertionError("input path must not run after activation failure")

    monkeypatch.setattr(
        mcp_module,
        "_handlers",
        SimpleNamespace(automation=automation, window=Window()),
    )
    monkeypatch.setattr(mcp_module, "_animate_cursor_to", fail_animate)

    result = await create_mcp_server().call_tool(
        "computer_click",
        {
            "window": {"id": 456, "pid": 123},
            "coordinate_space": "screen",
            "x": 10,
            "y": 20,
        },
    )

    assert result.structured_content["success"] is False
    assert "Failed to activate target window 456" in result.structured_content["error"]


@pytest.mark.asyncio
async def test_legacy_click_window_ids_do_not_auto_activate(monkeypatch):
    import computer_server.mcp_server as mcp_module

    window = _FakeWindow()

    async def noop_animate(_x: int, _y: int) -> None:
        return None

    monkeypatch.setattr(
        mcp_module,
        "_handlers",
        SimpleNamespace(automation=_FakeAutomation(), window=window),
    )
    monkeypatch.setattr(mcp_module, "_animate_cursor_to", noop_animate)

    result = await create_mcp_server().call_tool(
        "computer_click",
        {
            "pid": 123,
            "window_id": 456,
            "coordinate_space": "screen",
            "x": 10,
            "y": 20,
        },
    )

    assert result.structured_content["success"] is True
    assert "target_window_activated" not in result.structured_content
    assert window.activations == []


@pytest.mark.asyncio
async def test_recording_status_uses_merged_tool():
    result = await create_mcp_server().call_tool(
        "computer_recording",
        {"action": "status"},
    )

    assert result.structured_content == {"active": False, "turn_count": 0}


@pytest.mark.asyncio
async def test_learn_status_uses_merged_tool():
    result = await create_mcp_server().call_tool(
        "computer_learn",
        {"action": "status"},
    )

    assert result.structured_content["success"] is True
    data = result.structured_content["data"]
    assert data["recording"] is False
    assert data["mode"] == "off"
    assert data["action_count"] == 0
    assert data["duration_seconds"] == 0
    assert data["observation_count"] == 0
    assert data["pending_observations"] == 0
    assert data["degraded_observations"] == 0
    assert data["capture_warning_count"] == 0
    assert data["target_profile_counts"] == {}


@pytest.mark.asyncio
async def test_get_window_rehydrates_canonical_window(monkeypatch):
    import computer_server.services.window_identity_service as identity

    def fake_rehydrate_window(window_id, app=None):
        assert window_id == 456
        assert app == "Target.exe"
        return {
            "success": True,
            "id": 456,
            "window_id": 456,
            "pid": 123,
            "title": "Target",
            "window": {
                "id": 456,
                "window_id": 456,
                "pid": 123,
                "app": "Target.exe",
                "title": "Target",
            },
        }

    monkeypatch.setattr(identity, "rehydrate_window", fake_rehydrate_window)

    result = await create_mcp_server().call_tool(
        "computer_get_window",
        {"id": 456, "app": "Target.exe"},
    )

    assert result.structured_content == {
        "success": True,
        "id": 456,
        "window_id": 456,
        "pid": 123,
        "title": "Target",
        "window": {
            "id": 456,
            "window_id": 456,
            "pid": 123,
            "app": "Target.exe",
            "title": "Target",
        },
    }


@pytest.mark.asyncio
async def test_get_window_rejects_invalid_id(monkeypatch):
    import computer_server.services.window_identity_service as identity

    def fake_rehydrate_window(_window_id, app=None):
        raise ValueError("id must be an integer")

    monkeypatch.setattr(identity, "rehydrate_window", fake_rehydrate_window)

    result = await create_mcp_server().call_tool(
        "computer_get_window",
        {"id": 456},
    )

    assert result.structured_content == {
        "success": False,
        "error": "id must be an integer",
    }


@pytest.mark.asyncio
async def test_get_window_state_accepts_window_and_returns_window(monkeypatch):
    import computer_server.mcp_server as mcp_module
    import computer_server.services.window_state_service as window_state_service
    from computer_server.handlers.element_cache import get_element_cache

    async def fake_get_window_state_snapshot(
        _automation_handler,
        pid,
        window_id,
        **_kwargs,
    ):
        assert pid == 123
        assert window_id == 456
        cache = get_element_cache()
        cache.set_window_origin(123, 456, 10, 20)
        return {
            "success": True,
            "node_count": 0,
            "unfiltered_node_count": 0,
            "nodes": [],
            "nodes_coordinate_space": "window_resized",
            "tree_markdown": "",
            "screen_width": 100,
            "screen_height": 50,
            "capture_metadata": {"capture_backend": "wgc"},
            "_capture_geometry": {
                "compression_ratio": 2.0,
                "original_width": 200,
                "original_height": 100,
                "window_origin_x": 10,
                "window_origin_y": 20,
                "crop_origin_screen_x": 10,
                "crop_origin_screen_y": 20,
            },
        }

    cache = get_element_cache()
    cache.clear()
    monkeypatch.setattr(cache, "_is_window_valid", lambda _window_id: True)
    monkeypatch.setattr(
        window_state_service,
        "get_window_state_snapshot",
        fake_get_window_state_snapshot,
    )
    monkeypatch.setattr(
        mcp_module,
        "_handlers",
        SimpleNamespace(automation=_FakeAutomation()),
    )

    result = await create_mcp_server().call_tool(
        "computer_get_window_state",
        {"window": {"id": 456, "pid": 123, "title": "Target"}},
    )

    content = result.structured_content
    assert content["success"] is True
    assert content["snapshot_id"].startswith("snap-")
    assert content["screenshot_id"].startswith("shot-")
    assert content["capture"]["id"] == content["screenshot_id"]
    assert content["capture"]["snapshot_id"] == content["snapshot_id"]
    assert content["capture"]["screen_width"] == 100
    assert content["capture"]["screen_height"] == 50
    assert content["capture"]["capture_backend"] == "wgc"
    assert content["nodes_coordinate_space"] == "window_resized"
    assert content["screenshots"][0]["id"] == content["screenshot_id"]
    assert content["screenshots"][0]["role"] == "target"
    assert content["screenshots"][0]["zIndex"] == 0
    assert content["screenshots"][0]["originX"] == 10
    assert content["screenshots"][0]["originY"] == 20
    assert content["window"]["id"] == 456
    assert content["window"]["window_id"] == 456
    assert content["window"]["pid"] == 123
    assert content["window_ref"]["version"] == 1
    assert content["window_ref"]["pid"] == 123
    assert content["window_ref"]["window_id"] == 456
    assert content["agent_state"]["text_limit"] == 500
    assert content["agent_state"]["show_full_text"] is False
    assert content["state_text"] == content["agent_state"]["text"]
    assert "snapshot_id=" in content["state_text"]


@pytest.mark.asyncio
async def test_get_window_state_validates_agent_text_options(monkeypatch):
    import computer_server.mcp_server as mcp_module

    monkeypatch.setattr(
        mcp_module,
        "_handlers",
        SimpleNamespace(automation=_FakeAutomation()),
    )

    result = await create_mcp_server().call_tool(
        "computer_get_window_state",
        {
            "pid": 123,
            "window_id": 456,
            "agent_text_limit": 20001,
        },
    )

    assert result.structured_content == {
        "success": False,
        "error": "agent_text_limit must be between 0 and 20000",
    }


@pytest.mark.asyncio
async def test_get_window_state_registers_related_transient_screenshots(monkeypatch):
    import computer_server.mcp_server as mcp_module
    import computer_server.services.window_state_service as window_state_service

    async def noop_animate(_x: int, _y: int) -> None:
        return None

    async def fake_get_window_state_snapshot(
        _automation_handler,
        pid,
        window_id,
        **_kwargs,
    ):
        return {
            "success": True,
            "screenshot": "target.jpg",
            "node_count": 0,
            "unfiltered_node_count": 0,
            "nodes": [],
            "tree_markdown": "",
            "screen_width": 100,
            "screen_height": 50,
            "capture_metadata": {"capture_backend": "wgc"},
            "_capture_geometry": {
                "compression_ratio": 1.0,
                "window_origin_x": 10,
                "window_origin_y": 20,
                "crop_origin_screen_x": 10,
                "crop_origin_screen_y": 20,
            },
            "_related_screenshots": [
                {
                    "screenshot": "popup.jpg",
                    "pid": pid,
                    "window_id": 789,
                    "title": "Popup",
                    "z_index": 2,
                    "screen_width": 40,
                    "screen_height": 30,
                    "capture_metadata": {"capture_backend": "wgc"},
                    "_capture_geometry": {
                        "compression_ratio": 1.0,
                        "window_origin_x": 30,
                        "window_origin_y": 40,
                        "crop_origin_screen_x": 30,
                        "crop_origin_screen_y": 40,
                    },
                }
            ],
        }

    monkeypatch.setattr(
        window_state_service,
        "get_window_state_snapshot",
        fake_get_window_state_snapshot,
    )
    monkeypatch.setattr(
        mcp_module,
        "_handlers",
        SimpleNamespace(automation=_FakeAutomation()),
    )
    monkeypatch.setattr(mcp_module, "_scale_x", 1.0)
    monkeypatch.setattr(mcp_module, "_scale_y", 1.0)
    monkeypatch.setattr(mcp_module, "_animate_cursor_to", noop_animate)

    mcp = create_mcp_server()
    result = await mcp.call_tool(
        "computer_get_window_state",
        {"pid": 123, "window_id": 456},
    )

    content = result.structured_content
    assert len(content["screenshots"]) == 2
    popup = content["screenshots"][1]
    assert popup["role"] == "transient"
    assert popup["zIndex"] == 2
    assert popup["window_id"] == 789
    assert popup["parent_window_id"] == 456
    assert popup["path"] == "popup.jpg"
    assert popup["originX"] == 30
    assert popup["originY"] == 40
    assert popup["capture"]["capture_backend"] == "wgc"
    assert popup["screenshot_id"].startswith("shot-")

    click = await mcp.call_tool(
        "computer_click",
        {
            "screenshot_id": popup["screenshot_id"],
            "x": 5,
            "y": 6,
        },
    )
    assert click.structured_content["success"] is True
    assert click.structured_content["x"] == 35
    assert click.structured_content["y"] == 46


@pytest.mark.asyncio
async def test_snapshot_id_rejects_window_mismatch(monkeypatch):
    import computer_server.mcp_server as mcp_module
    import computer_server.services.window_state_service as window_state_service
    from computer_server.handlers.element_cache import get_element_cache

    async def fake_get_window_state_snapshot(
        _automation_handler,
        pid,
        window_id,
        **_kwargs,
    ):
        cache = get_element_cache()
        cache.set_window_origin(pid, window_id, 10, 20)
        return {
            "success": True,
            "node_count": 0,
            "unfiltered_node_count": 0,
            "nodes": [],
            "tree_markdown": "",
        }

    cache = get_element_cache()
    cache.clear()
    monkeypatch.setattr(cache, "_is_window_valid", lambda _window_id: True)
    monkeypatch.setattr(
        window_state_service,
        "get_window_state_snapshot",
        fake_get_window_state_snapshot,
    )
    monkeypatch.setattr(
        mcp_module,
        "_handlers",
        SimpleNamespace(automation=_FakeAutomation()),
    )
    mcp = create_mcp_server()

    snapshot_result = await mcp.call_tool(
        "computer_get_window_state",
        {"pid": 123, "window_id": 456},
    )
    snapshot_id = snapshot_result.structured_content["snapshot_id"]
    result = await mcp.call_tool(
        "computer_click",
        {"snapshot_id": snapshot_id, "window": {"id": 789, "pid": 123}, "x": 1, "y": 2},
    )

    assert result.structured_content == {
        "success": False,
        "error": "snapshot_id window_id mismatch: snapshot has window_id=456, got window_id=789",
    }


@pytest.mark.asyncio
async def test_screenshot_id_binds_click_coordinates_to_capture(monkeypatch):
    import computer_server.mcp_server as mcp_module
    import computer_server.services.window_state_service as window_state_service
    from computer_server.handlers.element_cache import get_element_cache

    async def noop_animate(_x: int, _y: int) -> None:
        return None

    async def fake_get_window_state_snapshot(
        _automation_handler,
        pid,
        window_id,
        **_kwargs,
    ):
        cache = get_element_cache()
        cache.set_window_origin(pid, window_id, 10, 20)
        return {
            "success": True,
            "node_count": 0,
            "unfiltered_node_count": 0,
            "nodes": [],
            "tree_markdown": "",
            "screen_width": 100,
            "screen_height": 50,
            "capture_metadata": {"capture_backend": "wgc"},
            "_capture_geometry": {
                "compression_ratio": 2.0,
                "original_width": 200,
                "original_height": 100,
                "window_origin_x": 10,
                "window_origin_y": 20,
                "crop_origin_screen_x": 10,
                "crop_origin_screen_y": 20,
            },
        }

    cache = get_element_cache()
    cache.clear()
    fake_automation = _FakeAutomation()
    monkeypatch.setattr(cache, "_is_window_valid", lambda _window_id: True)
    monkeypatch.setattr(mcp_module, "_zoom_metadata", {})
    monkeypatch.setattr(mcp_module, "_scale_x", 1.0)
    monkeypatch.setattr(mcp_module, "_scale_y", 1.0)
    monkeypatch.setattr(mcp_module, "_animate_cursor_to", noop_animate)
    monkeypatch.setattr(
        window_state_service,
        "get_window_state_snapshot",
        fake_get_window_state_snapshot,
    )
    monkeypatch.setattr(
        mcp_module,
        "_handlers",
        SimpleNamespace(automation=fake_automation),
    )
    mcp = create_mcp_server()

    snapshot_result = await mcp.call_tool(
        "computer_get_window_state",
        {"pid": 123, "window_id": 456},
    )
    screenshot_id = snapshot_result.structured_content["screenshot_id"]
    mcp_module._zoom_metadata[(123, 456)] = {
        "compression_ratio": 10.0,
        "crop_origin_screen_x": 1000,
        "crop_origin_screen_y": 2000,
    }

    result = await mcp.call_tool(
        "computer_click",
        {"screenshot_id": screenshot_id, "x": 5, "y": 6},
    )

    content = result.structured_content
    assert content["success"] is True
    assert content["x"] == 20
    assert content["y"] == 32
    assert content["action_path"]["input_screenshot_id"] == screenshot_id
    assert content["action_path"]["capture_backend"] == "wgc"
    assert content["action_path"]["capture_coordinate_space"] == "window_resized"


@pytest.mark.asyncio
async def test_screenshot_id_binds_drag_and_scroll_coordinates(monkeypatch):
    import computer_server.mcp_server as mcp_module
    import computer_server.services.window_state_service as window_state_service
    from computer_server.handlers.element_cache import get_element_cache

    async def noop_animate(_x: int, _y: int) -> None:
        return None

    async def fake_get_window_state_snapshot(
        _automation_handler,
        pid,
        window_id,
        **_kwargs,
    ):
        cache = get_element_cache()
        cache.set_window_origin(pid, window_id, 10, 20)
        return {
            "success": True,
            "node_count": 0,
            "unfiltered_node_count": 0,
            "nodes": [],
            "tree_markdown": "",
            "screen_width": 100,
            "screen_height": 50,
            "capture_metadata": {"capture_backend": "screen_crop"},
            "_capture_geometry": {
                "compression_ratio": 2.0,
                "original_width": 200,
                "original_height": 100,
                "window_origin_x": 10,
                "window_origin_y": 20,
                "crop_origin_screen_x": 10,
                "crop_origin_screen_y": 20,
            },
        }

    cache = get_element_cache()
    cache.clear()
    fake_automation = _FakeAutomation()
    monkeypatch.setattr(cache, "_is_window_valid", lambda _window_id: True)
    monkeypatch.setattr(mcp_module, "_zoom_metadata", {})
    monkeypatch.setattr(mcp_module, "_scale_x", 1.0)
    monkeypatch.setattr(mcp_module, "_scale_y", 1.0)
    monkeypatch.setattr(mcp_module, "_animate_cursor_to", noop_animate)
    monkeypatch.setattr(
        window_state_service,
        "get_window_state_snapshot",
        fake_get_window_state_snapshot,
    )
    monkeypatch.setattr(
        mcp_module,
        "_handlers",
        SimpleNamespace(automation=fake_automation),
    )
    mcp = create_mcp_server()

    snapshot_result = await mcp.call_tool(
        "computer_get_window_state",
        {"pid": 123, "window_id": 456},
    )
    screenshot_id = snapshot_result.structured_content["screenshot_id"]
    mcp_module._zoom_metadata[(123, 456)] = {
        "compression_ratio": 10.0,
        "crop_origin_screen_x": 1000,
        "crop_origin_screen_y": 2000,
    }

    drag_result = await mcp.call_tool(
        "computer_drag",
        {
            "screenshot_id": screenshot_id,
            "start_x": 1,
            "start_y": 2,
            "end_x": 3,
            "end_y": 4,
            "duration": 0.001,
        },
    )
    scroll_result = await mcp.call_tool(
        "computer_scroll",
        {
            "screenshot_id": screenshot_id,
            "x": 5,
            "y": 6,
            "scroll_y": 1,
            "smooth": False,
        },
    )

    assert drag_result.structured_content["success"] is True
    assert fake_automation.mouse_events[0] == ("down", 12, 24, "left")
    assert fake_automation.mouse_events[-1] == ("up", 16, 28, "left")
    assert drag_result.structured_content["action_path"]["input_screenshot_id"] == screenshot_id
    assert scroll_result.structured_content["success"] is True
    assert fake_automation.moves[-1] == (20, 32)
    assert scroll_result.structured_content["action_path"]["input_screenshot_id"] == screenshot_id


@pytest.mark.asyncio
async def test_screenshot_id_rejects_snapshot_mismatch(monkeypatch):
    import computer_server.mcp_server as mcp_module
    import computer_server.services.window_state_service as window_state_service
    from computer_server.handlers.element_cache import get_element_cache

    async def fake_get_window_state_snapshot(
        _automation_handler,
        pid,
        window_id,
        **_kwargs,
    ):
        cache = get_element_cache()
        cache.set_window_origin(pid, window_id, 10, 20)
        return {
            "success": True,
            "node_count": 0,
            "unfiltered_node_count": 0,
            "nodes": [],
            "tree_markdown": "",
            "screen_width": 10,
            "screen_height": 10,
            "capture_metadata": {"capture_backend": "wgc"},
            "_capture_geometry": {
                "compression_ratio": 1.0,
                "window_origin_x": 10,
                "window_origin_y": 20,
                "crop_origin_screen_x": 10,
                "crop_origin_screen_y": 20,
            },
        }

    cache = get_element_cache()
    cache.clear()
    monkeypatch.setattr(cache, "_is_window_valid", lambda _window_id: True)
    monkeypatch.setattr(mcp_module, "_zoom_metadata", {})
    monkeypatch.setattr(
        window_state_service,
        "get_window_state_snapshot",
        fake_get_window_state_snapshot,
    )
    monkeypatch.setattr(
        mcp_module,
        "_handlers",
        SimpleNamespace(automation=_FakeAutomation()),
    )
    mcp = create_mcp_server()

    first = await mcp.call_tool(
        "computer_get_window_state",
        {"pid": 123, "window_id": 456},
    )
    second = await mcp.call_tool(
        "computer_get_window_state",
        {"pid": 123, "window_id": 789},
    )
    result = await mcp.call_tool(
        "computer_click",
        {
            "snapshot_id": first.structured_content["snapshot_id"],
            "screenshot_id": second.structured_content["screenshot_id"],
            "x": 1,
            "y": 2,
        },
    )

    assert result.structured_content == {
        "success": False,
        "error": (
            "screenshot_id snapshot mismatch: "
            f"screenshot has snapshot_id={second.structured_content['snapshot_id']}, "
            f"got snapshot_id={first.structured_content['snapshot_id']}"
        ),
    }


@pytest.mark.asyncio
async def test_screenshot_id_rejects_stale_capture(monkeypatch):
    import computer_server.mcp_server as mcp_module
    import computer_server.services.window_state_service as window_state_service
    from computer_server.handlers.element_cache import get_element_cache

    async def fake_get_window_state_snapshot(
        _automation_handler,
        pid,
        window_id,
        **_kwargs,
    ):
        cache = get_element_cache()
        cache.set_window_origin(pid, window_id, 10, 20)
        return {
            "success": True,
            "node_count": 0,
            "unfiltered_node_count": 0,
            "nodes": [],
            "tree_markdown": "",
            "screen_width": 10,
            "screen_height": 10,
            "capture_metadata": {"capture_backend": "wgc"},
            "_capture_geometry": {
                "compression_ratio": 1.0,
                "window_origin_x": 10,
                "window_origin_y": 20,
                "crop_origin_screen_x": 10,
                "crop_origin_screen_y": 20,
            },
        }

    cache = get_element_cache()
    cache.clear()
    monkeypatch.setattr(cache, "_is_window_valid", lambda _window_id: True)
    monkeypatch.setattr(mcp_module, "_zoom_metadata", {})
    monkeypatch.setattr(
        window_state_service,
        "get_window_state_snapshot",
        fake_get_window_state_snapshot,
    )
    monkeypatch.setattr(
        mcp_module,
        "_handlers",
        SimpleNamespace(automation=_FakeAutomation()),
    )
    mcp = create_mcp_server()

    first = await mcp.call_tool(
        "computer_get_window_state",
        {"pid": 123, "window_id": 456},
    )
    await mcp.call_tool(
        "computer_get_window_state",
        {"pid": 123, "window_id": 456},
    )
    result = await mcp.call_tool(
        "computer_click",
        {"screenshot_id": first.structured_content["screenshot_id"], "x": 1, "y": 2},
    )

    content = result.structured_content
    assert content["success"] is False
    assert content["code"] == "screenshot_stale"
    assert content["error"] == "screenshot_id is stale for pid=123, window_id=456"
    assert content["target_profile"]["family"]
    assert content["suggestion"] == (
        "Use the latest screenshot_id returned by computer_get_window_state or capture_after."
    )


@pytest.mark.asyncio
async def test_capture_after_does_not_build_delta_unless_requested(monkeypatch):
    import computer_server.mcp_server as mcp_module
    import computer_server.services.window_state_service as window_state_service

    async def noop_animate(_x: int, _y: int) -> None:
        return None

    calls = 0

    async def fake_get_window_state_snapshot(
        _automation_handler,
        pid,
        window_id,
        **_kwargs,
    ):
        nonlocal calls
        calls += 1
        return {
            "success": True,
            "node_count": 1,
            "unfiltered_node_count": 1,
            "returned_node_count": 1,
            "filtered_node_count": 1,
            "nodes": [
                {
                    "element_index": calls,
                    "control_type": "Text",
                    "name": f"state {calls}",
                }
            ],
            "tree_markdown": "",
            "screen_width": 10,
            "screen_height": 10,
            "capture_metadata": {"capture_backend": "wgc"},
            "_capture_geometry": {
                "compression_ratio": 1.0,
                "window_origin_x": 0,
                "window_origin_y": 0,
                "crop_origin_screen_x": 0,
                "crop_origin_screen_y": 0,
            },
        }

    monkeypatch.setattr(mcp_module, "_scale_x", 1.0)
    monkeypatch.setattr(mcp_module, "_scale_y", 1.0)
    monkeypatch.setattr(mcp_module, "_animate_cursor_to", noop_animate)
    monkeypatch.setattr(
        window_state_service,
        "get_window_state_snapshot",
        fake_get_window_state_snapshot,
    )
    monkeypatch.setattr(
        mcp_module,
        "_handlers",
        SimpleNamespace(automation=_FakeAutomation()),
    )
    mcp = create_mcp_server()

    default_result = await mcp.call_tool(
        "computer_click",
        {
            "x": 1,
            "y": 2,
            "pid": 123,
            "window_id": 456,
            "coordinate_space": "screen",
            "capture_after": True,
        },
    )
    assert default_result.structured_content["success"] is True
    assert calls == 1
    assert "capture_after" in default_result.structured_content
    assert "post_action_delta" not in default_result.structured_content

    delta_result = await mcp.call_tool(
        "computer_click",
        {
            "x": 1,
            "y": 2,
            "pid": 123,
            "window_id": 456,
            "coordinate_space": "screen",
            "capture_after": True,
            "include_post_action_delta": True,
        },
    )
    assert delta_result.structured_content["success"] is True
    assert calls == 3
    assert delta_result.structured_content["post_action_delta"]["success"] is True


@pytest.mark.asyncio
async def test_post_action_delta_requires_capture_after():
    result = await create_mcp_server().call_tool(
        "computer_click",
        {
            "x": 1,
            "y": 2,
            "include_post_action_delta": True,
        },
    )

    assert result.structured_content == {
        "success": False,
        "error": "include_post_action_delta=True requires capture_after=True",
    }


@pytest.mark.asyncio
async def test_zoom_uses_screenshot_id_capture_metadata(monkeypatch):
    import computer_server.mcp_server as mcp_module
    import computer_server.services.window_state_service as window_state_service
    from computer_server.handlers.element_cache import get_element_cache

    async def fake_get_window_state_snapshot(
        _automation_handler,
        pid,
        window_id,
        **_kwargs,
    ):
        cache = get_element_cache()
        cache.set_window_origin(pid, window_id, 10, 20)
        return {
            "success": True,
            "node_count": 0,
            "unfiltered_node_count": 0,
            "nodes": [],
            "tree_markdown": "",
            "screen_width": 100,
            "screen_height": 50,
            "capture_metadata": {"capture_backend": "wgc"},
            "_capture_geometry": {
                "compression_ratio": 2.0,
                "original_width": 200,
                "original_height": 100,
                "window_origin_x": 10,
                "window_origin_y": 20,
                "crop_origin_screen_x": 10,
                "crop_origin_screen_y": 20,
            },
        }

    async def fake_create_zoom_image(
        _automation_handler,
        pid,
        window_id,
        x1,
        y1,
        x2,
        y2,
        zoom_metadata,
    ):
        assert pid == 123
        assert window_id == 456
        assert (x1, y1, x2, y2) == (1, 2, 3, 4)
        assert zoom_metadata[(123, 456)]["compression_ratio"] == 2.0
        assert zoom_metadata[(123, 456)]["crop_origin_screen_x"] == 10
        assert zoom_metadata[(123, 456)]["capture_backend"] == "wgc"
        return {
            "success": True,
            "zoom_id": "zoom-test",
            "pid": pid,
            "window_id": window_id,
        }

    cache = get_element_cache()
    cache.clear()
    monkeypatch.setattr(cache, "_is_window_valid", lambda _window_id: True)
    monkeypatch.setattr(mcp_module, "_zoom_metadata", {})
    monkeypatch.setattr(
        window_state_service,
        "get_window_state_snapshot",
        fake_get_window_state_snapshot,
    )
    monkeypatch.setattr(
        window_state_service,
        "create_zoom_image",
        fake_create_zoom_image,
    )
    monkeypatch.setattr(
        mcp_module,
        "_handlers",
        SimpleNamespace(automation=_FakeAutomation()),
    )
    mcp = create_mcp_server()

    snapshot_result = await mcp.call_tool(
        "computer_get_window_state",
        {"pid": 123, "window_id": 456},
    )
    screenshot_id = snapshot_result.structured_content["screenshot_id"]
    mcp_module._zoom_metadata[(123, 456)] = {
        "compression_ratio": 10.0,
        "crop_origin_screen_x": 1000,
        "capture_backend": "screen_crop",
    }

    result = await mcp.call_tool(
        "computer_zoom",
        {
            "screenshot_id": screenshot_id,
            "x1": 1,
            "y1": 2,
            "x2": 3,
            "y2": 4,
        },
    )

    assert result.structured_content["success"] is True
    assert result.structured_content["parent_screenshot_id"] == screenshot_id


@pytest.mark.asyncio
async def test_set_value_falls_back_when_uia_action_times_out(monkeypatch):
    import time

    import computer_server.mcp_server as mcp_module
    from computer_server.handlers.element_cache import CachedElement, get_element_cache

    async def noop_animate(_x: int, _y: int) -> None:
        return None

    def slow_set_value(_x: int, _y: int, _value: str) -> None:
        time.sleep(0.2)

    cache = get_element_cache()
    cache.clear()
    monkeypatch.setattr(cache, "_is_window_valid", lambda _window_id: True)
    cache.update(
        123,
        456,
        [
            CachedElement(
                element_index=1,
                source="uia",
                center_x=5,
                center_y=6,
                bbox=(0, 0, 10, 12),
                control_type="Edit",
                name="Name",
                actions=["set_value"],
            )
        ],
    )
    cache.set_window_origin(123, 456, 10, 20)

    monkeypatch.setenv("CUA_UIA_ACTION_TIMEOUT", "0.01")
    monkeypatch.setattr(mcp_module, "_set_value_via_uia_point", slow_set_value)
    monkeypatch.setattr(mcp_module, "_animate_cursor_to", noop_animate)
    monkeypatch.setattr(
        mcp_module,
        "_handlers",
        SimpleNamespace(automation=_FakeAutomation(), window=_FakeWindow()),
    )

    result = await create_mcp_server().call_tool(
        "computer_set_value",
        {
            "window": {"id": 456, "pid": 123},
            "element_index": 1,
            "value": "abc",
        },
    )

    assert result.structured_content["success"] is True
    assert result.structured_content["method"] == "click_type"
