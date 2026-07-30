from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

os.environ["CUA_SKIP_SOM_WARMUP"] = "1"

import pytest
from fastmcp.exceptions import ToolError

from computer_server.handlers.element_cache import CachedElement, get_element_cache
from computer_server.mcp_server import create_mcp_server


def _content(result):
    """Expose tool-specific data while asserting the fixed 0.2 envelope."""
    envelope = result.structured_content
    assert set(envelope) == {
        "protocol_version",
        "success",
        "data",
        "artifacts",
        "warnings",
        "error",
        "action",
    }
    assert envelope["protocol_version"] == "0.2"
    if envelope["success"]:
        return {"success": True, **envelope["data"]}
    error = envelope["error"]
    return {
        "success": False,
        "error": error["message"],
        "code": error["code"],
        "suggestion": error["suggestion"],
        **error["details"],
    }


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
async def test_drag_runtime_value_error_releases_mouse_and_is_not_rejected(
    monkeypatch,
):
    import computer_server.mcp_server as mcp_module

    class FailingMouse:
        @property
        def position(self):
            return (0, 0)

        @position.setter
        def position(self, _value):
            raise ValueError("cursor delivery failed")

    class Automation(_FakeAutomation):
        def __init__(self):
            super().__init__()
            self.mouse = FailingMouse()
            self.button_down = False

        async def mouse_down(self, x, y, button="left"):
            self.button_down = True
            self.mouse_events.append(("down", x, y, button))
            return {"success": True}

        async def mouse_up(self, x=None, y=None, button="left"):
            self.button_down = False
            self.mouse_events.append(("up", x, y, button))
            return {"success": True}

    automation = Automation()
    monkeypatch.setattr(
        mcp_module,
        "_handlers",
        SimpleNamespace(automation=automation),
    )

    with pytest.raises(ToolError, match="cursor delivery failed"):
        await create_mcp_server().call_tool(
            "computer_drag",
            {
                "start_x": 1,
                "start_y": 1,
                "end_x": 2,
                "end_y": 2,
                "coordinate_space": "screen",
                "duration": 0.1,
            },
        )

    assert automation.button_down is False
    assert automation.mouse_events == [
        ("down", 1, 1, "left"),
        ("up", None, None, "left"),
    ]


@pytest.mark.asyncio
async def test_drag_release_failure_is_unknown_and_remains_recoverable(
    monkeypatch,
):
    import computer_server.mcp_server as mcp_module

    class Automation(_FakeAutomation):
        def __init__(self):
            super().__init__()
            self.release_attempts = 0

        async def mouse_up(self, x=None, y=None, button="left"):
            self.release_attempts += 1
            self.mouse_events.append(("up", x, y, button))
            if self.release_attempts == 1:
                return {"success": False, "error": "release failed"}
            return {"success": True}

    automation = Automation()
    monkeypatch.setattr(
        mcp_module,
        "_handlers",
        SimpleNamespace(automation=automation),
    )
    mcp = create_mcp_server()

    drag_result = await mcp.call_tool(
        "computer_drag",
        {
            "start_x": 1,
            "start_y": 1,
            "end_x": 2,
            "end_y": 2,
            "coordinate_space": "screen",
            "duration": 0.1,
        },
    )
    drag_envelope = drag_result.structured_content

    assert drag_envelope["success"] is False
    assert drag_envelope["error"]["code"] == "ACTION_OUTCOME_UNKNOWN"
    assert drag_envelope["action"]["outcome"] == "unknown"
    assert drag_envelope["action"]["retry_safety"] == "reobserve"

    release_result = await mcp.call_tool("computer_release_all_inputs", {})
    release_data = _content(release_result)

    assert release_data["success"] is True
    assert release_data["released_mouse_buttons"] == ["left"]
    assert automation.release_attempts == 2


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
    envelope = result.structured_content
    assert _content(result) == {"success": True}
    assert envelope["data"] == {}
    assert envelope["action"] is None
    assert envelope["artifacts"] == [
        {
            "id": envelope["artifacts"][0]["id"],
            "type": "image",
            "role": "screen",
            "mime_type": "image/png",
            "content_index": 0,
            "width": 32,
            "height": 24,
            "byte_count": len(base64.b64decode(result.content[0].data)),
        }
    ]


@pytest.mark.asyncio
async def test_screenshot_accepts_canonical_window_without_semantic_observation(
    monkeypatch,
):
    import base64

    from PIL import Image as PILImage

    import computer_server.mcp_server as mcp_module
    import computer_server.services.window_state_service as window_state_service
    from computer_server.handlers.element_cache import get_element_cache
    from computer_server.services.window_state_service import WindowSnapshotGeometry

    async def fake_capture_window_snapshot_image(
        _automation_handler,
        pid,
        window_id,
        *,
        max_image_dimension,
    ):
        assert pid == 123
        assert window_id == 456
        assert max_image_dimension == 640
        return {
            "success": True,
            "image": PILImage.new("RGB", (80, 40), "white"),
            "geometry": WindowSnapshotGeometry(
                full_width=80,
                full_height=40,
                screenshot_origin_x=10,
                screenshot_origin_y=20,
                window_origin_x=10,
                window_origin_y=20,
                crop_origin_screen_x=10,
                crop_origin_screen_y=20,
                crop_origin_window_x=0,
                crop_origin_window_y=0,
                original_width=80,
                original_height=40,
                screen_width=80,
                screen_height=40,
                compression_ratio=1.0,
            ),
            "capture_metadata": {
                "capture_backend": "wgc",
                "target": "window",
                "warnings": [],
            },
            "window_capture_state": {},
        }

    cache = get_element_cache()
    cache.clear()
    monkeypatch.setattr(cache, "_is_window_valid", lambda _window_id: True)
    monkeypatch.setattr(
        window_state_service,
        "capture_window_snapshot_image",
        fake_capture_window_snapshot_image,
    )
    monkeypatch.setattr(
        mcp_module,
        "_handlers",
        SimpleNamespace(automation=_FakeAutomation()),
    )

    result = await create_mcp_server().call_tool(
        "computer_screenshot",
        {
            "format": "png",
            "window": {"id": 456, "pid": 123},
            "max_image_dimension": 640,
        },
    )

    assert len(result.content) == 1
    assert result.content[0].type == "image"
    assert result.content[0].mimeType == "image/png"
    envelope = result.structured_content
    assert envelope["success"] is True
    assert envelope["data"] == {}
    assert envelope["artifacts"] == [
        {
            "id": envelope["artifacts"][0]["id"],
            "type": "image",
            "role": "target",
            "mime_type": "image/png",
            "content_index": 0,
            "width": 80,
            "height": 40,
            "byte_count": len(base64.b64decode(result.content[0].data)),
            "window": {"id": 456, "pid": 123},
        }
    ]


class _FakeWindow:
    def __init__(self):
        self.activations = []

    async def get_current_window_id(self):
        return {
            "success": True,
            "window_id": 456,
            "pid": 123,
            "name": "Target Window",
        }

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

    content = _content(result)
    assert content["success"] is True
    assert content["window_ready"] is True
    assert content["launch_pid"] == 111
    assert content["window"] == {"id": 456, "pid": 222}
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

    content = _content(result)
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

    content = _content(result)
    assert content["success"] is True
    assert content["already_running"] is True
    assert content["window_ready"] is True
    assert content["window"] == {"id": 456, "pid": 222}
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
    window_state = next(
        tool for tool in tools if tool.name == "computer_get_window_state"
    )
    secondary = next(
        tool for tool in tools if tool.name == "computer_perform_secondary_action"
    )
    assert "action" not in click.parameters["properties"]
    assert "computer_double_click" not in window_state.description
    assert "computer_click (use count=2 for double-click)" in window_state.description
    assert set(secondary.parameters["required"]) == {"element_index", "action"}
    screenshot = next(tool for tool in tools if tool.name == "computer_screenshot")
    assert "window" in screenshot.parameters["properties"]
    assert "max_image_dimension" in screenshot.parameters["properties"]
    assert "window_id" not in screenshot.parameters["properties"]
    assert "UIA/MSAA" in screenshot.description
    manage_window = next(tool for tool in tools if tool.name == "computer_manage_window")
    manage_action = manage_window.parameters["properties"]["action"]
    assert "restores minimized targets" in manage_action["description"]
    assert "without injecting synthetic key input" in manage_action["description"]


@pytest.mark.asyncio
async def test_public_tool_descriptions_match_protocol_02():
    tools = await create_mcp_server().list_tools()
    by_name = {tool.name: tool for tool in tools}

    for tool in tools:
        for parameter, schema in tool.parameters.get("properties", {}).items():
            assert schema.get("description"), f"{tool.name}.{parameter} has no description"

    assert "targetable top-level windows" in by_name["computer_list_apps"].description
    assert "sampled turns" in by_name["computer_recording"].description
    assert "strict DesktopStopResult" in by_name["computer_learn"].description
    assert (
        "excluded_window_ids"
        not in by_name["computer_learn"].parameters["properties"]
    )
    assert "latest screenshot identified by screenshot_id" in (
        by_name["computer_zoom"].description
    )


@pytest.mark.asyncio
async def test_open_url_rejects_local_paths():
    result = await create_mcp_server().call_tool(
        "computer_open_url",
        {"url": "C:/Windows"},
    )

    assert _content(result) == {
        "success": False,
        "error": "url must be an absolute http or https URL",
        "code": "INVALID_ARGUMENT",
        "suggestion": None,
    }


@pytest.mark.asyncio
async def test_click_requires_coordinates_or_element():
    result = await create_mcp_server().call_tool("computer_click", {})

    assert _content(result) == {
        "success": False,
        "error": "x and y are required unless element_index is provided",
        "code": "INVALID_ARGUMENT",
        "suggestion": (
            "Provide x/y coordinates, or pass window, observation_id, "
            "and element_index from computer_get_window_state."
        ),
    }


@pytest.mark.asyncio
async def test_click_rejects_empty_verify_query_before_action():
    result = await create_mcp_server().call_tool(
        "computer_click",
        {"x": 1, "y": 1, "verify_query": "   "},
    )

    assert _content(result) == {
        "success": False,
        "error": "verify_query must not be empty",
        "code": "INVALID_ARGUMENT",
        "suggestion": None,
    }


@pytest.mark.asyncio
async def test_click_require_verification_requires_query():
    result = await create_mcp_server().call_tool(
        "computer_click",
        {"x": 1, "y": 1, "require_verification": True},
    )

    assert _content(result) == {
        "success": False,
        "error": "require_verification=True requires verify_query",
        "code": "INVALID_ARGUMENT",
        "suggestion": None,
    }


@pytest.mark.asyncio
async def test_click_returns_action_path_for_pixel_click(monkeypatch):
    import computer_server.mcp_server as mcp_module

    async def noop_animate(_x: int, _y: int) -> None:
        return None

    monkeypatch.setattr(
        mcp_module,
        "_handlers",
        SimpleNamespace(automation=_FakeAutomation(), window=_FakeWindow()),
    )
    monkeypatch.setattr(mcp_module, "_animate_cursor_to", noop_animate)

    result = await create_mcp_server().call_tool(
        "computer_click",
        {"x": 1, "y": 2},
    )

    assert _content(result)["success"] is True
    assert _content(result)["method"] == "pixel_click"
    action_path = _content(result)["action_path"]
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
    import computer_server.services.target_profile_service as target_profile_service

    automation = _FakeAutomation()
    window_handler = _FakeWindow()
    monkeypatch.setattr(
        mcp_module,
        "_handlers",
        SimpleNamespace(automation=automation, window=window_handler),
    )
    monkeypatch.setattr(
        target_profile_service,
        "_process_name_from_pid",
        lambda _pid: "pwsh.exe",
    )
    monkeypatch.setattr(
        target_profile_service,
        "_class_name_from_hwnd",
        lambda _window_id: "CASCADIA_HOSTING_WINDOW_CLASS",
    )

    result = await create_mcp_server().call_tool(
        "computer_type",
        {
            "text": "hello",
            "window": {"id": 456, "pid": 123},
        },
    )

    action_path = _content(result)["action_path"]
    assert action_path["method"] == "clipboard_type"
    assert action_path["target_profile"]["family"] == "terminal"
    assert action_path["delivery_risk"] == "terminal_focus_sensitive"
    assert automation.typed_text == ["hello"]
    assert window_handler.activations == [456]


@pytest.mark.asyncio
async def test_type_preserves_locked_desktop_preflight_rejection(monkeypatch):
    import computer_server.mcp_server as mcp_module
    from computer_server.handlers.windows import require_unlocked_desktop

    class LockedAutomation(_FakeAutomation):
        def is_desktop_locked(self):
            return True

        @require_unlocked_desktop
        async def type_text(self, text: str):
            raise AssertionError("locked desktop must reject before input")

    monkeypatch.setattr(
        mcp_module,
        "_handlers",
        SimpleNamespace(automation=LockedAutomation()),
    )

    result = await create_mcp_server().call_tool(
        "computer_type",
        {"text": "hello"},
    )
    envelope = result.structured_content

    assert envelope["success"] is False
    assert envelope["error"]["code"] == "ACTION_REJECTED"
    assert envelope["action"]["outcome"] == "rejected"
    assert envelope["action"]["retry_safety"] == "safe"


@pytest.mark.asyncio
async def test_press_key_returns_target_profile_action_path(monkeypatch):
    import computer_server.mcp_server as mcp_module
    import computer_server.services.target_profile_service as target_profile_service

    automation = _FakeAutomation()
    window_handler = _FakeWindow()
    monkeypatch.setattr(
        mcp_module,
        "_handlers",
        SimpleNamespace(automation=automation, window=window_handler),
    )
    monkeypatch.setattr(
        target_profile_service,
        "_process_name_from_pid",
        lambda _pid: "soffice.bin",
    )
    monkeypatch.setattr(
        target_profile_service,
        "_class_name_from_hwnd",
        lambda _window_id: "SALFRAME",
    )

    result = await create_mcp_server().call_tool(
        "computer_press_key",
        {
            "key": "Control_L+s",
            "window": {"id": 456, "pid": 123},
        },
    )

    action_path = _content(result)["action_path"]
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

    assert _content(result) == {
        "success": False,
        "error": "window is required",
        "code": "TARGET_REQUIRED",
        "suggestion": None,
    }


@pytest.mark.asyncio
async def test_secondary_action_rejects_non_secondary_action():
    result = await create_mcp_server().call_tool(
        "computer_perform_secondary_action",
        {"element_index": 1, "action": "raise"},
    )

    assert _content(result) == {
        "success": False,
        "error": "action must be one of: expand",
        "code": "INVALID_ARGUMENT",
        "suggestion": None,
    }


@pytest.mark.asyncio
async def test_secondary_action_expands_msaa_dropdown(monkeypatch):
    import computer_server.mcp_server as mcp_module
    import computer_server.services.window_state_service as window_state_service

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

    async def fake_get_window_state_snapshot(
        _automation_handler,
        _pid,
        _window_id,
        **_kwargs,
    ):
        return {
            "success": True,
            "node_count": 1,
            "unfiltered_node_count": 1,
            "nodes": [],
            "tree_markdown": "",
        }

    monkeypatch.setattr(
        window_state_service,
        "get_window_state_snapshot",
        fake_get_window_state_snapshot,
    )
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

        mcp = create_mcp_server()
        observation = await mcp.call_tool(
            "computer_get_window_state",
            {"window": {"id": 456, "pid": 123}},
        )
        result = await mcp.call_tool(
            "computer_perform_secondary_action",
            {
                "element_index": 7,
                "action": "expand",
                "window": {"id": 456, "pid": 123},
                "observation_id": _content(observation)["observation_id"],
            },
        )

        content = _content(result)
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

    window_record = _content(result)
    assert "id" not in window_record
    assert window_record["window"]["id"] == 456
    assert window_record["name"] == "Target Window"


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
        {
            "window": {"id": 456, "pid": 123},
            "action": "resize",
            "width": 800,
            "height": 600,
        },
    )

    assert _content(activate_result)["success"] is True
    assert _content(activate_result)["action"] == "activate"
    assert _content(resize_result)["success"] is True
    assert _content(resize_result)["window"] == {"id": 456, "pid": 123}
    assert _content(resize_result)["width"] == 800
    assert _content(resize_result)["height"] == 600
    assert _content(resize_result)["action"] == "resize"


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

    record = _content(all_windows)["windows"][0]
    assert record["minimized"] is True
    assert record["is_on_screen"] is False
    assert record["window"] == {"id": 456, "pid": 123}
    assert _content(on_screen)["windows"] == []


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

    apps = _content(result)["apps"]
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

    windows = _content(result)["windows"]
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

    assert _content(move_result) == {"success": True, "x": 10, "y": 20}
    assert _content(down_result) == {
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

    content = _content(result)
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

    assert _content(result)["success"] is True
    assert _content(result)["window"] == {"id": 456, "pid": 123}
    assert _content(result)["target_window_activated"] is True
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

    assert _content(result)["success"] is False
    assert "Failed to activate target window 456" in _content(result)["error"]
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

    assert _content(result)["success"] is True
    assert _content(result)["target_window_activated"] is False
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

    assert _content(down_result)["success"] is True
    assert _content(down_result)["key"] == "Shift"
    assert _content(down_result)["action"] == "down"
    assert _content(down_result)["action_path"]["method"] == "key_down"
    assert _content(up_result)["success"] is True
    assert _content(up_result)["key"] == "Shift"
    assert _content(up_result)["action"] == "up"
    assert _content(up_result)["action_path"]["method"] == "key_up"
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
    assert _content(click)["target_window_activated"] is True

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
    assert _content(drag)["target_window_activated"] is True

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
    assert _content(scroll)["target_window_activated"] is True

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
    assert _content(smooth_scroll)["success"] is True
    assert _content(smooth_scroll)["target_window_activated"] is True


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

    assert _content(result)["success"] is False
    assert "Failed to activate target window 456" in _content(result)["error"]


@pytest.mark.asyncio
async def test_click_schema_removes_legacy_window_ids():
    tools = await create_mcp_server().list_tools()
    click = next(tool for tool in tools if tool.name == "computer_click")
    properties = click.parameters["properties"]

    assert "window" in properties
    assert "pid" not in properties
    assert "window_id" not in properties


@pytest.mark.asyncio
async def test_recording_status_uses_merged_tool():
    result = await create_mcp_server().call_tool(
        "computer_recording",
        {"action": "status"},
    )

    assert _content(result) == {
        "success": True,
        "active": False,
        "turn_count": 0,
    }


@pytest.mark.asyncio
async def test_learn_status_uses_merged_tool():
    result = await create_mcp_server().call_tool(
        "computer_learn",
        {"action": "status"},
    )

    assert _content(result)["success"] is True
    data = _content(result)
    assert data["teaching_contract"] == "desktop-action.v1"
    assert data["stop_barrier"] == "cutoff-sequence.v1"
    assert "source_window_exclusion" not in data
    assert data["recording"] is False
    assert data["mode"] == "off"
    assert data["action_count"] == 0
    assert data["duration_seconds"] == 0
    assert data["observation_count"] == 0
    assert data["pending_observations"] == 0
    assert data["degraded_observations"] == 0
    assert data["capture_warning_count"] == 0
    assert data["target_profile_counts"] == {}
    assert data["actions"] == []
    assert data["session_id"] is None
    assert data["next_sequence"] == 0
    assert data["timeline_truncated"] is False


@pytest.mark.asyncio
async def test_learn_status_validates_incremental_timeline_arguments():
    result = await create_mcp_server().call_tool(
        "computer_learn",
        {
            "action": "status",
            "after_sequence": -1,
            "timeline_limit": 201,
        },
    )

    content = _content(result)
    assert content["success"] is False
    assert "after_sequence must be between" in content["error"]


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

    assert _content(result) == {
        "success": True,
        "title": "Target",
        "window": {"id": 456, "pid": 123},
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

    assert _content(result) == {
        "success": False,
        "error": "id must be an integer",
        "code": "INVALID_ARGUMENT",
        "suggestion": None,
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
        {"window": {"id": 456, "pid": 123}},
    )

    content = _content(result)
    assert content["success"] is True
    assert content["observation_id"].startswith("obs-")
    assert content["screenshot_id"].startswith("shot-")
    assert content["capture"]["id"] == content["screenshot_id"]
    assert content["capture"]["observation_id"] == content["observation_id"]
    assert content["capture"]["screen_width"] == 100
    assert content["capture"]["screen_height"] == 50
    assert content["capture"]["capture_backend"] == "wgc"
    assert content["nodes_coordinate_space"] == "window_resized"
    assert content["window"] == {"id": 456, "pid": 123}
    assert content["target_profile"]["family"]
    assert "screenshots" not in content
    assert "window_ref" not in content
    assert content["agent_state"]["text_limit"] == 500
    assert content["agent_state"]["show_full_text"] is False
    assert content["state_text"] == content["agent_state"]["text"]
    assert "observation_id=" in content["state_text"]


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
            "window": {"id": 456, "pid": 123},
            "agent_text_limit": 20001,
        },
    )

    assert _content(result) == {
        "success": False,
        "error": "agent_text_limit must be between 0 and 20000",
        "code": "INVALID_ARGUMENT",
        "suggestion": None,
    }


@pytest.mark.asyncio
async def test_get_window_state_registers_related_transient_screenshots(
    monkeypatch,
    tmp_path,
):
    import computer_server.mcp_server as mcp_module
    import computer_server.services.window_state_service as window_state_service
    from PIL import Image

    target_path = tmp_path / "target.jpg"
    popup_path = tmp_path / "popup.jpg"
    Image.new("RGB", (100, 50), "white").save(target_path)
    Image.new("RGB", (40, 30), "white").save(popup_path)

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
            "screenshot": str(target_path),
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
                    "screenshot": str(popup_path),
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
        SimpleNamespace(automation=_FakeAutomation(), window=_FakeWindow()),
    )
    monkeypatch.setattr(mcp_module, "_scale_x", 1.0)
    monkeypatch.setattr(mcp_module, "_scale_y", 1.0)
    monkeypatch.setattr(mcp_module, "_animate_cursor_to", noop_animate)

    mcp = create_mcp_server()
    result = await mcp.call_tool(
        "computer_get_window_state",
        {"window": {"id": 456, "pid": 123}},
    )

    content = _content(result)
    assert "screenshots" not in content
    assert len(result.content) == 2
    artifacts = result.structured_content["artifacts"]
    assert len(artifacts) == 2
    assert artifacts[0]["role"] == "target"
    popup = artifacts[1]
    assert popup["role"] == "transient"
    assert popup["window"] == {"id": 789, "pid": 123}
    assert popup["observation_id"] == content["observation_id"]
    assert popup["content_index"] == 1
    assert popup["screenshot_id"].startswith("shot-")

    click = await mcp.call_tool(
        "computer_click",
        {
            "window": {"id": 789, "pid": 123},
            "screenshot_id": popup["screenshot_id"],
            "coordinate_space": "window_resized",
            "x": 5,
            "y": 6,
        },
    )
    assert _content(click)["success"] is True
    assert _content(click)["x"] == 35
    assert _content(click)["y"] == 46


@pytest.mark.asyncio
async def test_observation_id_rejects_window_mismatch(monkeypatch):
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

    observation_result = await mcp.call_tool(
        "computer_get_window_state",
        {"window": {"id": 456, "pid": 123}},
    )
    observation_id = _content(observation_result)["observation_id"]
    result = await mcp.call_tool(
        "computer_click",
        {
            "observation_id": observation_id,
            "window": {"id": 789, "pid": 123},
            "coordinate_space": "screen",
            "x": 1,
            "y": 2,
        },
    )

    assert _content(result) == {
        "success": False,
        "error": (
            "observation_id target mismatch: observation has window=456, "
            "got window=789"
        ),
        "code": "OBSERVATION_TARGET_MISMATCH",
        "suggestion": None,
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
        SimpleNamespace(automation=fake_automation, window=_FakeWindow()),
    )
    mcp = create_mcp_server()

    observation_result = await mcp.call_tool(
        "computer_get_window_state",
        {"window": {"id": 456, "pid": 123}},
    )
    screenshot_id = _content(observation_result)["screenshot_id"]
    mcp_module._zoom_metadata[(123, 456)] = {
        "compression_ratio": 10.0,
        "crop_origin_screen_x": 1000,
        "crop_origin_screen_y": 2000,
    }

    result = await mcp.call_tool(
        "computer_click",
        {
            "window": {"id": 456, "pid": 123},
            "screenshot_id": screenshot_id,
            "coordinate_space": "window_resized",
            "x": 5,
            "y": 6,
        },
    )

    content = _content(result)
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
        SimpleNamespace(automation=fake_automation, window=_FakeWindow()),
    )
    mcp = create_mcp_server()

    observation_result = await mcp.call_tool(
        "computer_get_window_state",
        {"window": {"id": 456, "pid": 123}},
    )
    screenshot_id = _content(observation_result)["screenshot_id"]
    mcp_module._zoom_metadata[(123, 456)] = {
        "compression_ratio": 10.0,
        "crop_origin_screen_x": 1000,
        "crop_origin_screen_y": 2000,
    }

    drag_result = await mcp.call_tool(
        "computer_drag",
        {
            "window": {"id": 456, "pid": 123},
            "screenshot_id": screenshot_id,
            "coordinate_space": "window_resized",
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
            "window": {"id": 456, "pid": 123},
            "screenshot_id": screenshot_id,
            "coordinate_space": "window_resized",
            "x": 5,
            "y": 6,
            "scroll_y": 1,
            "smooth": False,
        },
    )

    assert _content(drag_result)["success"] is True
    assert fake_automation.mouse_events[0] == ("down", 12, 24, "left")
    assert fake_automation.mouse_events[-1] == ("up", 16, 28, "left")
    assert _content(drag_result)["action_path"]["input_screenshot_id"] == screenshot_id
    assert _content(scroll_result)["success"] is True
    assert fake_automation.moves[-1] == (20, 32)
    assert _content(scroll_result)["action_path"]["input_screenshot_id"] == screenshot_id


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
        SimpleNamespace(automation=_FakeAutomation(), window=_FakeWindow()),
    )
    mcp = create_mcp_server()

    first = await mcp.call_tool(
        "computer_get_window_state",
        {"window": {"id": 456, "pid": 123}},
    )
    second = await mcp.call_tool(
        "computer_get_window_state",
        {"window": {"id": 789, "pid": 123}},
    )
    result = await mcp.call_tool(
        "computer_click",
        {
            "window": {"id": 456, "pid": 123},
            "observation_id": _content(first)["observation_id"],
            "screenshot_id": _content(second)["screenshot_id"],
            "coordinate_space": "window_resized",
            "x": 1,
            "y": 2,
        },
    )

    assert _content(result) == {
        "success": False,
        "error": (
            "screenshot_id observation mismatch: "
            f"screenshot has observation_id={_content(second)['observation_id']}, "
            f"got observation_id={_content(first)['observation_id']}"
        ),
        "code": "OBSERVATION_TARGET_MISMATCH",
        "suggestion": None,
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
        {"window": {"id": 456, "pid": 123}},
    )
    await mcp.call_tool(
        "computer_get_window_state",
        {"window": {"id": 456, "pid": 123}},
    )
    result = await mcp.call_tool(
        "computer_click",
        {
            "window": {"id": 456, "pid": 123},
            "screenshot_id": _content(first)["screenshot_id"],
            "coordinate_space": "window_resized",
            "x": 1,
            "y": 2,
        },
    )

    content = _content(result)
    assert content["success"] is False
    assert content["code"] == "STALE_OBSERVATION"
    assert content["error"] == "screenshot_id is stale for pid=123, window=456"
    assert content["target_profile"]["family"]
    assert content["suggestion"] == (
        "Use the latest screenshot_id returned by computer_get_window_state or capture_after."
    )


@pytest.mark.asyncio
async def test_click_rechecks_screenshot_freshness_before_dispatch(monkeypatch):
    import computer_server.mcp_server as mcp_module
    import computer_server.services.window_state_service as window_state_service

    activation_started = asyncio.Event()
    release_activation = asyncio.Event()

    class BlockingWindow(_FakeWindow):
        async def activate_window(self, window_id: int):
            activation_started.set()
            await release_activation.wait()
            return {"success": True, "window_id": window_id}

    class Automation(_FakeAutomation):
        def __init__(self):
            super().__init__()
            self.clicks = []

        async def left_click(self, x: int, y: int):
            self.clicks.append((x, y))
            return {"success": True, "x": x, "y": y}

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

    automation = Automation()
    monkeypatch.setattr(mcp_module, "_animate_cursor_to", noop_animate)
    monkeypatch.setattr(
        window_state_service,
        "get_window_state_snapshot",
        fake_get_window_state_snapshot,
    )
    monkeypatch.setattr(
        mcp_module,
        "_handlers",
        SimpleNamespace(
            automation=automation,
            window=BlockingWindow(),
        ),
    )
    mcp = create_mcp_server()
    first = await mcp.call_tool(
        "computer_get_window_state",
        {"window": {"id": 456, "pid": 123}},
    )
    first_screenshot_id = _content(first)["screenshot_id"]

    click_task = asyncio.create_task(
        mcp.call_tool(
            "computer_click",
            {
                "window": {"id": 456, "pid": 123},
                "screenshot_id": first_screenshot_id,
                "coordinate_space": "window_resized",
                "x": 5,
                "y": 6,
            },
        )
    )
    await activation_started.wait()
    await mcp.call_tool(
        "computer_get_window_state",
        {"window": {"id": 456, "pid": 123}},
    )
    release_activation.set()
    result = await click_task

    assert result.structured_content["success"] is False
    assert result.structured_content["error"]["code"] == "STALE_OBSERVATION"
    assert result.structured_content["action"]["outcome"] == "rejected"
    assert automation.clicks == []


@pytest.mark.asyncio
async def test_uia_click_rechecks_freshness_inside_worker(monkeypatch):
    import computer_server.mcp_server as mcp_module
    import computer_server.services.window_state_service as window_state_service

    gate_entered = asyncio.Event()
    release_gate = asyncio.Event()
    dispatches = []

    async def blocking_gate(*, func, **_kwargs):
        gate_entered.set()
        await release_gate.wait()
        return func(), None

    async def noop_animate(_x: int, _y: int) -> None:
        return None

    async def fake_get_window_state_snapshot(
        _automation_handler,
        _pid,
        _window_id,
        **_kwargs,
    ):
        return {
            "success": True,
            "node_count": 1,
            "unfiltered_node_count": 1,
            "nodes": [],
            "tree_markdown": "",
            "screen_width": 20,
            "screen_height": 20,
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
    monkeypatch.setattr(mcp_module, "_animate_cursor_to", noop_animate)
    monkeypatch.setattr(mcp_module, "_window_snapshot", lambda: (0, set()))
    monkeypatch.setattr(
        mcp_module,
        "_invoke_via_uia_point",
        lambda x, y, on_dispatch=None: dispatches.append((x, y)),
    )
    monkeypatch.setattr(
        window_state_service,
        "get_window_state_snapshot",
        fake_get_window_state_snapshot,
    )
    monkeypatch.setattr(
        window_state_service,
        "run_window_operation_with_gate",
        blocking_gate,
    )
    monkeypatch.setattr(
        mcp_module,
        "_handlers",
        SimpleNamespace(
            automation=_FakeAutomation(),
            window=_FakeWindow(),
        ),
    )

    mcp = create_mcp_server()
    first = await mcp.call_tool(
        "computer_get_window_state",
        {"window": {"id": 456, "pid": 123}},
    )
    cache.set_window_origin(123, 456, 10, 20)
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
                control_type="Button",
                name="Run",
                actions=["invoke"],
                enabled=True,
            )
        ],
    )

    click_task = asyncio.create_task(
        mcp.call_tool(
            "computer_click",
            {
                "window": {"id": 456, "pid": 123},
                "observation_id": _content(first)["observation_id"],
                "element_index": 1,
            },
        )
    )
    await gate_entered.wait()
    await mcp.call_tool(
        "computer_get_window_state",
        {"window": {"id": 456, "pid": 123}},
    )
    release_gate.set()
    result = await click_task

    assert result.structured_content["success"] is False
    assert result.structured_content["error"]["code"] == "STALE_OBSERVATION"
    assert result.structured_content["action"]["outcome"] == "rejected"
    assert dispatches == []


@pytest.mark.asyncio
async def test_uia_click_unsupported_before_dispatch_falls_back_to_pixel(
    monkeypatch,
):
    import computer_server.mcp_server as mcp_module
    import computer_server.services.window_state_service as window_state_service
    from computer_server.handlers.windows_uia import UIAPatternUnavailableError

    async def noop_animate(_x: int, _y: int) -> None:
        return None

    async def fake_get_window_state_snapshot(
        _automation_handler,
        _pid,
        _window_id,
        **_kwargs,
    ):
        return {
            "success": True,
            "node_count": 1,
            "unfiltered_node_count": 1,
            "nodes": [
                {
                    "element_index": 1,
                    "source": "uia",
                    "center_x": 5,
                    "center_y": 6,
                    "bbox": (0, 0, 10, 12),
                    "control_type": "Text",
                    "name": "Run",
                    "actions": ["invoke"],
                    "enabled": True,
                }
            ],
            "tree_markdown": "",
            "screen_width": 20,
            "screen_height": 20,
            "_capture_geometry": {
                "compression_ratio": 1.0,
                "window_origin_x": 10,
                "window_origin_y": 20,
                "crop_origin_screen_x": 10,
                "crop_origin_screen_y": 20,
            },
        }

    def unsupported_invoke(_x: int, _y: int, *, on_dispatch=None) -> None:
        raise UIAPatternUnavailableError("InvokePattern is unsupported")

    cache = get_element_cache()
    cache.clear()
    monkeypatch.setattr(cache, "_is_window_valid", lambda _window_id: True)
    monkeypatch.setattr(mcp_module, "_animate_cursor_to", noop_animate)
    monkeypatch.setattr(mcp_module, "_window_snapshot", lambda: (0, set()))
    monkeypatch.setattr(
        mcp_module,
        "_invoke_via_uia_point",
        unsupported_invoke,
    )
    monkeypatch.setattr(
        window_state_service,
        "get_window_state_snapshot",
        fake_get_window_state_snapshot,
    )
    automation = _FakeAutomation()
    monkeypatch.setattr(
        mcp_module,
        "_handlers",
        SimpleNamespace(automation=automation, window=_FakeWindow()),
    )

    mcp = create_mcp_server()
    observation = await mcp.call_tool(
        "computer_get_window_state",
        {"window": {"id": 456, "pid": 123}},
    )
    cache.set_window_origin(123, 456, 10, 20)
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
                control_type="Text",
                name="Run",
                actions=["invoke"],
                enabled=True,
            )
        ],
    )
    result = await mcp.call_tool(
        "computer_click",
        {
            "window": {"id": 456, "pid": 123},
            "observation_id": _content(observation)["observation_id"],
            "element_index": 1,
        },
    )

    content = _content(result)
    assert content["success"] is True, content
    assert content["action_path"]["method"] == "element_center_pixel_click"


@pytest.mark.asyncio
async def test_uia_click_stays_completed_when_window_change_telemetry_fails(
    monkeypatch,
):
    import win32process

    import computer_server.mcp_server as mcp_module
    import computer_server.services.window_state_service as window_state_service

    dispatches = []

    async def noop_animate(_x: int, _y: int) -> None:
        return None

    async def fake_get_window_state_snapshot(
        _automation_handler,
        _pid,
        _window_id,
        **_kwargs,
    ):
        return {
            "success": True,
            "node_count": 1,
            "unfiltered_node_count": 1,
            "nodes": [],
            "tree_markdown": "",
            "screen_width": 20,
            "screen_height": 20,
            "capture_metadata": {"capture_backend": "wgc"},
            "_capture_geometry": {
                "compression_ratio": 1.0,
                "window_origin_x": 10,
                "window_origin_y": 20,
                "crop_origin_screen_x": 10,
                "crop_origin_screen_y": 20,
            },
        }

    def successful_invoke(x: int, y: int, *, on_dispatch=None) -> None:
        if on_dispatch is not None:
            on_dispatch()
        dispatches.append((x, y))

    def unavailable_process_id(_hwnd):
        raise RuntimeError("window disappeared")

    cache = get_element_cache()
    cache.clear()
    monkeypatch.setattr(cache, "_is_window_valid", lambda _window_id: True)
    monkeypatch.setattr(mcp_module, "_animate_cursor_to", noop_animate)
    monkeypatch.setattr(mcp_module, "_window_snapshot", lambda: (100, frozenset({100})))
    monkeypatch.setattr(mcp_module, "_invoke_via_uia_point", successful_invoke)
    monkeypatch.setattr(
        win32process,
        "GetWindowThreadProcessId",
        unavailable_process_id,
    )
    monkeypatch.setattr(
        window_state_service,
        "get_window_state_snapshot",
        fake_get_window_state_snapshot,
    )
    monkeypatch.setattr(
        mcp_module,
        "_handlers",
        SimpleNamespace(automation=_FakeAutomation(), window=_FakeWindow()),
    )

    mcp = create_mcp_server()
    observation = await mcp.call_tool(
        "computer_get_window_state",
        {"window": {"id": 456, "pid": 123}},
    )
    cache.set_window_origin(123, 456, 10, 20)
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
                control_type="Button",
                name="Run",
                actions=["invoke"],
                enabled=True,
            )
        ],
    )

    result = await mcp.call_tool(
        "computer_click",
        {
            "window": {"id": 456, "pid": 123},
            "observation_id": _content(observation)["observation_id"],
            "element_index": 1,
        },
    )

    assert result.structured_content["success"] is True
    assert result.structured_content["action"]["outcome"] == "completed"
    assert result.structured_content["data"]["method"] == "uia_invoke"
    assert dispatches == [(15, 26)]


@pytest.mark.asyncio
async def test_set_value_unsupported_before_dispatch_falls_back_to_keyboard(
    monkeypatch,
):
    import computer_server.mcp_server as mcp_module
    import computer_server.services.window_state_service as window_state_service
    from computer_server.handlers.windows_uia import UIAPatternUnavailableError

    async def noop_animate(_x: int, _y: int) -> None:
        return None

    async def fake_get_window_state_snapshot(
        _automation_handler,
        _pid,
        _window_id,
        **_kwargs,
    ):
        return {
            "success": True,
            "node_count": 1,
            "unfiltered_node_count": 1,
            "nodes": [
                {
                    "element_index": 1,
                    "source": "uia",
                    "center_x": 5,
                    "center_y": 6,
                    "bbox": (0, 0, 10, 12),
                    "control_type": "Text",
                    "name": "Name",
                    "actions": ["set_value"],
                    "enabled": True,
                }
            ],
            "tree_markdown": "",
            "screen_width": 20,
            "screen_height": 20,
            "_capture_geometry": {
                "compression_ratio": 1.0,
                "window_origin_x": 10,
                "window_origin_y": 20,
                "crop_origin_screen_x": 10,
                "crop_origin_screen_y": 20,
            },
        }

    def unsupported_set_value(
        _x: int,
        _y: int,
        _value: str,
        *,
        on_dispatch=None,
    ) -> None:
        raise UIAPatternUnavailableError("ValuePattern is unsupported")

    cache = get_element_cache()
    cache.clear()
    monkeypatch.setattr(cache, "_is_window_valid", lambda _window_id: True)
    monkeypatch.setattr(mcp_module, "_animate_cursor_to", noop_animate)
    monkeypatch.setattr(
        mcp_module,
        "_set_value_via_uia_point",
        unsupported_set_value,
    )
    monkeypatch.setattr(
        window_state_service,
        "get_window_state_snapshot",
        fake_get_window_state_snapshot,
    )
    automation = _FakeAutomation()
    monkeypatch.setattr(
        mcp_module,
        "_handlers",
        SimpleNamespace(automation=automation, window=_FakeWindow()),
    )

    mcp = create_mcp_server()
    observation = await mcp.call_tool(
        "computer_get_window_state",
        {"window": {"id": 456, "pid": 123}},
    )
    cache.set_window_origin(123, 456, 10, 20)
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
                control_type="Text",
                name="Name",
                actions=["set_value"],
                enabled=True,
            )
        ],
    )
    result = await mcp.call_tool(
        "computer_set_value",
        {
            "window": {"id": 456, "pid": 123},
            "observation_id": _content(observation)["observation_id"],
            "element_index": 1,
            "value": "中文",
        },
    )

    content = _content(result)
    assert content["success"] is True, content
    assert content["method"] == "click_type"
    assert automation.key_events == [("hotkey", ["ctrl", "a"])]
    assert automation.typed_text == ["中文"]

    cache.update(
        123,
        456,
        [
            CachedElement(
                element_index=2,
                source="uia",
                center_x=5,
                center_y=6,
                bbox=(0, 0, 10, 12),
                control_type="Image",
                name="Logo",
                actions=[],
                enabled=True,
            )
        ],
    )
    unsupported = await mcp.call_tool(
        "computer_set_value",
        {
            "window": {"id": 456, "pid": 123},
            "observation_id": _content(observation)["observation_id"],
            "element_index": 2,
            "value": "x",
        },
    )

    assert _content(unsupported)["code"] == "UNSUPPORTED_OPERATION"
    assert unsupported.structured_content["action"]["outcome"] == "rejected"


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
        SimpleNamespace(automation=_FakeAutomation(), window=_FakeWindow()),
    )
    mcp = create_mcp_server()

    default_result = await mcp.call_tool(
        "computer_click",
        {
            "x": 1,
            "y": 2,
            "window": {"id": 456, "pid": 123},
            "coordinate_space": "screen",
            "capture_after": True,
        },
    )
    assert _content(default_result)["success"] is True
    assert calls == 1
    assert "capture_after" in _content(default_result)
    assert "post_action_delta" not in _content(default_result)

    delta_result = await mcp.call_tool(
        "computer_click",
        {
            "x": 1,
            "y": 2,
            "window": {"id": 456, "pid": 123},
            "coordinate_space": "screen",
            "capture_after": True,
            "include_post_action_delta": True,
        },
    )
    assert _content(delta_result)["success"] is True
    assert calls == 3
    assert _content(delta_result)["post_action_delta"]["success"] is True

    verified_result = await mcp.call_tool(
        "computer_click",
        {
            "x": 1,
            "y": 2,
            "window": {"id": 456, "pid": 123},
            "coordinate_space": "screen",
            "capture_after": True,
            "verify_query": "state",
            "require_verification": True,
        },
    )
    verified = _content(verified_result)
    assert verified["success"] is True
    assert calls == 4
    assert verified["post_action_verification"]["matched"] is True
    assert (
        verified["post_action_verification"]["observation_id"]
        == verified["capture_after"]["observation_id"]
    )
    assert (
        verified["post_action_verification"]["screenshot_id"]
        == verified["capture_after"]["screenshot_id"]
    )


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

    assert _content(result) == {
        "success": False,
        "error": "include_post_action_delta=True requires capture_after=True",
        "code": "INVALID_ARGUMENT",
        "suggestion": None,
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

    observation_result = await mcp.call_tool(
        "computer_get_window_state",
        {"window": {"id": 456, "pid": 123}},
    )
    screenshot_id = _content(observation_result)["screenshot_id"]
    mcp_module._zoom_metadata[(123, 456)] = {
        "compression_ratio": 10.0,
        "crop_origin_screen_x": 1000,
        "capture_backend": "screen_crop",
    }

    result = await mcp.call_tool(
        "computer_zoom",
        {
            "window": {"id": 456, "pid": 123},
            "screenshot_id": screenshot_id,
            "x1": 1,
            "y1": 2,
            "x2": 3,
            "y2": 4,
        },
    )

    assert _content(result)["success"] is True
    assert _content(result)["parent_screenshot_id"] == screenshot_id


@pytest.mark.asyncio
async def test_set_value_reports_unknown_when_uia_action_times_out(monkeypatch):
    import time

    import computer_server.mcp_server as mcp_module
    from computer_server.handlers.element_cache import CachedElement, get_element_cache
    import computer_server.services.window_state_service as window_state_service

    async def noop_animate(_x: int, _y: int) -> None:
        return None

    def slow_set_value(
        _x: int,
        _y: int,
        _value: str,
        *,
        on_dispatch=None,
    ) -> None:
        if on_dispatch is not None:
            on_dispatch()
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

    async def fake_get_window_state_snapshot(
        _automation_handler,
        _pid,
        _window_id,
        **_kwargs,
    ):
        return {
            "success": True,
            "node_count": 1,
            "unfiltered_node_count": 1,
            "nodes": [],
            "tree_markdown": "",
        }

    monkeypatch.setattr(
        window_state_service,
        "get_window_state_snapshot",
        fake_get_window_state_snapshot,
    )
    mcp = create_mcp_server()
    observation = await mcp.call_tool(
        "computer_get_window_state",
        {"window": {"id": 456, "pid": 123}},
    )
    result = await mcp.call_tool(
        "computer_set_value",
        {
            "window": {"id": 456, "pid": 123},
            "observation_id": _content(observation)["observation_id"],
            "element_index": 1,
            "value": "abc",
        },
    )

    assert _content(result)["success"] is False
    assert _content(result)["code"] == "ACTION_OUTCOME_UNKNOWN"
    assert result.structured_content["action"]["outcome"] == "unknown"
    assert result.structured_content["action"]["retry_safety"] == "reobserve"
