from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

os.environ["CUA_SKIP_SOM_WARMUP"] = "1"

from computer_server.handlers.element_cache import CachedElement, get_element_cache
from computer_server.mcp_server import create_mcp_server


class _Automation:
    def __init__(self) -> None:
        self.mouse_events: list[tuple[str, str]] = []
        self.key_events: list[tuple[str, str]] = []
        self.typed_text: list[str] = []
        self.clicks: list[tuple[int, int]] = []

    async def mouse_down(self, x=None, y=None, button: str = "left"):
        self.mouse_events.append(("down", button))
        return {"success": True}

    async def mouse_up(self, x=None, y=None, button: str = "left"):
        self.mouse_events.append(("up", button))
        return {"success": True}

    async def press_key(self, key: str):
        self.key_events.append(("press", key))
        return {"success": True}

    async def type_text(self, text: str):
        self.typed_text.append(text)
        return {"success": True}

    async def left_click(self, x: int, y: int):
        self.clicks.append((x, y))
        return {"success": True, "x": x, "y": y}

    async def right_click(self, x: int, y: int):
        return {"success": True, "x": x, "y": y}

    async def middle_click(self, x: int, y: int):
        return {"success": True, "x": x, "y": y}

    async def key_up(self, key: str):
        self.key_events.append(("up", key))
        return {"success": True}


class _Window:
    def __init__(self) -> None:
        self.actions: list[tuple[str, int]] = []

    async def activate_window(self, window_id: int):
        self.actions.append(("activate", window_id))
        return {"success": True, "window_id": window_id}


@pytest.mark.asyncio
async def test_recording_replay_uses_current_consolidated_tool_names(
    tmp_path,
    monkeypatch,
):
    import computer_server.mcp_server as mcp_module
    import computer_server.services.replay_resolver as replay_resolver

    automation = _Automation()
    window = _Window()
    monkeypatch.setattr(
        mcp_module,
        "_handlers",
        SimpleNamespace(automation=automation, window=window),
    )
    monkeypatch.setattr(
        replay_resolver,
        "resolve_live_window",
        lambda _source: (456, (0, 0, 100, 100)),
    )

    mcp = create_mcp_server()
    start = await mcp.call_tool(
        "computer_recording",
        {
            "action": "start",
            "output_dir": str(tmp_path),
            "capture_screenshots": False,
        },
    )
    assert start.structured_content["success"] is True

    await mcp.call_tool(
        "computer_mouse",
        {"action": "down", "button": "left"},
    )
    await mcp.call_tool(
        "computer_mouse",
        {"action": "up", "button": "left"},
    )
    await mcp.call_tool(
        "computer_press_key",
        {"key": "shift", "window": {"id": 456, "pid": 123}},
    )
    await mcp.call_tool(
        "computer_type",
        {"text": "hello", "window": {"id": 456, "pid": 123}},
    )
    await mcp.call_tool(
        "computer_manage_window",
        {"action": "activate", "window_id": 456},
    )

    stop = await mcp.call_tool("computer_recording", {"action": "stop"})
    assert stop.structured_content["success"] is True

    action_files = sorted(tmp_path.glob("turn-*/action.json"))
    actions = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in action_files
    ]
    assert [action["tool"] for action in actions] == [
        "computer_mouse",
        "computer_mouse",
        "computer_press_key",
        "computer_type",
        "computer_manage_window",
    ]
    keyboard_actions = actions[2:4]
    assert [action["args"]["window_id"] for action in keyboard_actions] == [456, 456]
    assert [action["args"]["pid"] for action in keyboard_actions] == [123, 123]

    automation.mouse_events.clear()
    automation.key_events.clear()
    automation.typed_text.clear()
    window.actions.clear()

    replay = await mcp.call_tool(
        "computer_replay_trajectory",
        {
            "recording_dir": str(tmp_path),
            "delay_ms": 0,
        },
    )

    assert replay.structured_content == {
        "turns_replayed": 5,
        "turns_failed": 0,
        "turns_total": 5,
        "completed": True,
        "errors": [],
    }
    assert automation.mouse_events == [("down", "left"), ("up", "left")]
    assert automation.key_events == [("press", "shift")]
    assert automation.typed_text == ["hello"]
    assert window.actions == [
        ("activate", 456),
        ("activate", 456),
        ("activate", 456),
    ]


@pytest.mark.asyncio
async def test_recording_replay_secondary_action_uses_current_element(
    tmp_path,
    monkeypatch,
):
    import computer_server.mcp_server as mcp_module
    import computer_server.services.replay_resolver as replay_resolver

    async def noop_animate(_x: int, _y: int) -> None:
        return None

    automation = _Automation()
    window = _Window()
    cache = get_element_cache()
    cache.clear()
    monkeypatch.setattr(cache, "_is_window_valid", lambda _window_id: True)
    monkeypatch.setattr(
        mcp_module,
        "_handlers",
        SimpleNamespace(automation=automation, window=window),
    )
    monkeypatch.setattr(mcp_module, "_animate_cursor_to", noop_animate)
    monkeypatch.setattr(
        replay_resolver,
        "resolve_live_window",
        lambda _source: (456, (100, 200, 500, 500)),
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
        start = await mcp.call_tool(
            "computer_recording",
            {
                "action": "start",
                "output_dir": str(tmp_path),
                "capture_screenshots": False,
            },
        )
        assert start.structured_content["success"] is True

        action = await mcp.call_tool(
            "computer_perform_secondary_action",
            {
                "element_index": 7,
                "action": "expand",
                "pid": 123,
                "window_id": 456,
            },
        )
        assert action.structured_content["success"] is True

        stop = await mcp.call_tool("computer_recording", {"action": "stop"})
        assert stop.structured_content["success"] is True

        recorded = json.loads(
            next(tmp_path.glob("turn-*/action.json")).read_text(encoding="utf-8")
        )
        assert recorded["tool"] == "computer_perform_secondary_action"
        assert recorded["args"]["action"] == "expand"
        assert recorded["element_ref"]["name"] == "字体颜色"

        automation.clicks.clear()
        window.actions.clear()
        replay = await mcp.call_tool(
            "computer_replay_trajectory",
            {
                "recording_dir": str(tmp_path),
                "delay_ms": 0,
            },
        )

        assert replay.structured_content == {
            "turns_replayed": 1,
            "turns_failed": 0,
            "turns_total": 1,
            "completed": True,
            "errors": [],
        }
        assert automation.clicks == [(156, 220)]
        assert window.actions == [("activate", 456)]
    finally:
        cache.clear()
