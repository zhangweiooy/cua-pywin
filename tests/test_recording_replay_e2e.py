from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

os.environ["CUA_SKIP_SOM_WARMUP"] = "1"

from computer_server.mcp_server import create_mcp_server


class _Automation:
    def __init__(self) -> None:
        self.mouse_events: list[tuple[str, str]] = []
        self.key_events: list[tuple[str, str]] = []

    async def mouse_down(self, x=None, y=None, button: str = "left"):
        self.mouse_events.append(("down", button))
        return {"success": True}

    async def mouse_up(self, x=None, y=None, button: str = "left"):
        self.mouse_events.append(("up", button))
        return {"success": True}

    async def press_key(self, key: str):
        self.key_events.append(("press", key))
        return {"success": True}

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
        {"key": "shift"},
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
        "computer_manage_window",
    ]

    automation.mouse_events.clear()
    automation.key_events.clear()
    window.actions.clear()

    replay = await mcp.call_tool(
        "computer_replay_trajectory",
        {
            "recording_dir": str(tmp_path),
            "delay_ms": 0,
        },
    )

    assert replay.structured_content == {
        "turns_replayed": 4,
        "turns_failed": 0,
        "turns_total": 4,
        "completed": True,
        "errors": [],
    }
    assert automation.mouse_events == [("down", "left"), ("up", "left")]
    assert automation.key_events == [("press", "shift")]
    assert window.actions == [("activate", 456)]
