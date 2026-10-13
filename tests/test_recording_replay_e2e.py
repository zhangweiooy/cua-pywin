from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

os.environ["CUA_SKIP_SOM_WARMUP"] = "1"

from computer_server.handlers.element_cache import CachedElement, get_element_cache
from computer_server.mcp_server import create_mcp_server


def _data(result):
    envelope = result.structured_content
    assert envelope["protocol_version"] == "0.2"
    assert envelope["success"] is True, envelope
    return envelope["data"]


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
@pytest.mark.parametrize("recorded_name", [None, "send"])
async def test_replay_rejects_unidentifiable_icon_without_sending_input(
    tmp_path, monkeypatch, recorded_name,
):
    import computer_server.mcp_server as mcp_module
    import computer_server.services.window_state_service as window_state_service

    automation = _Automation()
    window = _Window()
    monkeypatch.setattr(
        mcp_module, "_handlers",
        SimpleNamespace(automation=automation, window=window),
    )
    elements = [
        CachedElement(
            element_index=index, source="vision", control_type="icon",
            name=None, actions=["click"], center_x=x, center_y=100,
            bbox=(x - 10, 90, x + 10, 110),
        )
        for index, x in [(10000, 100), (10001, 200)]
    ]
    monkeypatch.setattr(get_element_cache(), "get_elements", lambda *_: elements)

    snapshot_calls = []

    async def snapshot(*args, **kwargs):
        snapshot_calls.append(True)
        return {
            "success": True, "nodes": [], "node_count": 0,
            "unfiltered_node_count": 0, "tree_markdown": "",
        }

    monkeypatch.setattr(window_state_service, "get_window_state_snapshot", snapshot)
    (tmp_path / "recording.json").write_text(
        json.dumps({"protocol_version": "0.2"}), encoding="utf-8",
    )
    actions = [
        {
            "tool": "computer_click",
            "args": {
                "window": {"id": 456, "pid": 123}, "element_index": 10042,
                "x": 100, "y": 100,
            },
            "element_ref": {
                "source": "vision", "control_type": "icon",
                "name": recorded_name, "actions": ["click"],
                "center_x": 100, "center_y": 100,
            },
        },
        {"tool": "computer_mouse", "args": {"action": "down", "button": "left"}},
    ]
    for index, action in enumerate(actions, 1):
        turn = tmp_path / f"turn-{index:05d}"
        turn.mkdir()
        (turn / "action.json").write_text(
            json.dumps({"protocol_version": "0.2", **action}), encoding="utf-8",
        )

    result = await create_mcp_server().call_tool(
        "computer_replay_trajectory", {"recording_dir": str(tmp_path), "delay_ms": 0},
    )
    envelope = result.structured_content
    assert envelope["success"] is False
    assert envelope["error"]["code"] == "ACTION_REJECTED"
    assert "select the target again" in envelope["error"]["message"]
    assert envelope["action"]["outcome"] == "rejected"
    details = envelope["error"]["details"]
    assert details["turns_replayed"] == 0
    assert details["turns_failed"] == 1
    assert details["turns_total"] == 2
    assert automation.clicks == []
    assert automation.mouse_events == []
    assert window.actions == []
    assert snapshot_calls == []


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
    _data(start)

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
        {"action": "activate", "window": {"id": 456, "pid": 123}},
    )

    stop = await mcp.call_tool("computer_recording", {"action": "stop"})
    _data(stop)
    manifest = json.loads(
        (tmp_path / "recording.json").read_text(encoding="utf-8")
    )
    assert manifest["protocol_version"] == "0.2"

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
    assert [action["args"]["window"] for action in keyboard_actions] == [
        {"id": 456, "pid": 123},
        {"id": 456, "pid": 123},
    ]
    assert all(action["protocol_version"] == "0.2" for action in actions)

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

    assert _data(replay) == {
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
    import computer_server.services.window_identity_service as window_identity
    import computer_server.services.window_state_service as window_state_service

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
    monkeypatch.setattr(
        window_identity,
        "build_window",
        lambda window_id, **_kwargs: {"id": int(window_id), "pid": 123},
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
        _data(start)

        observation = await mcp.call_tool(
            "computer_get_window_state",
            {"window": {"id": 456, "pid": 123}},
        )

        action = await mcp.call_tool(
            "computer_perform_secondary_action",
            {
                "element_index": 7,
                "action": "expand",
                "window": {"id": 456, "pid": 123},
                "observation_id": _data(observation)["observation_id"],
            },
        )
        _data(action)

        stop = await mcp.call_tool("computer_recording", {"action": "stop"})
        _data(stop)

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

        assert _data(replay) == {
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
