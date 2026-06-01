from __future__ import annotations

import os
from types import SimpleNamespace

os.environ["CUA_SKIP_SOM_WARMUP"] = "1"

import pytest

from computer_server.mcp_server import create_mcp_server


class _FakeAutomation:
    async def move_cursor(self, x: int, y: int):
        return {"success": True, "x": x, "y": y}

    async def left_click(self, x: int, y: int):
        return {"success": True, "x": x, "y": y}

    async def right_click(self, x: int, y: int):
        return {"success": True, "x": x, "y": y}

    async def middle_click(self, x: int, y: int):
        return {"success": True, "x": x, "y": y}

    async def hotkey(self, keys):
        return {"success": True, "keys": keys}

    async def type_text(self, text: str):
        return {"success": True, "text": text}


class _FakeWindow:
    async def activate_window(self, window_id: int):
        return {"success": True, "window_id": window_id}


REMOVED_PUBLIC_TOOLS = {
    "computer_file_read",
    "computer_file_write",
    "computer_file_exists",
    "computer_directory_exists",
    "computer_list_directory",
    "computer_create_directory",
    "computer_delete_file",
    "computer_delete_directory",
    "computer_get_file_size",
    "computer_run_command",
    "computer_set_wallpaper",
    "computer_clipboard_get",
    "computer_clipboard_set",
    "computer_open",
}


@pytest.mark.asyncio
async def test_agent_native_tools_are_not_exposed():
    mcp = create_mcp_server()
    tools = await mcp.list_tools()
    names = {tool.name for tool in tools}

    assert len(names) == 42
    assert names.isdisjoint(REMOVED_PUBLIC_TOOLS)
    assert "computer_open_url" in names


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
    assert result.structured_content["action_path"] == {
        "method": "pixel_click",
        "used_foreground": True,
        "uses_system_input": True,
        "requires_visible_target": True,
        "coordinate_source": "screen",
    }


@pytest.mark.asyncio
async def test_click_raise_requires_element_index():
    result = await create_mcp_server().call_tool(
        "computer_click",
        {"action": "raise"},
    )

    assert result.structured_content == {
        "success": False,
        "error": "action='raise' or action='expand' is only supported with element_index",
    }


@pytest.mark.asyncio
async def test_click_expand_requires_element_index():
    result = await create_mcp_server().call_tool(
        "computer_click",
        {"action": "expand"},
    )

    assert result.structured_content == {
        "success": False,
        "error": "action='raise' or action='expand' is only supported with element_index",
    }


@pytest.mark.asyncio
async def test_accessibility_tree_uses_protected_service(monkeypatch):
    import computer_server.services.window_state_service as window_state_service

    async def fake_get_accessibility_tree_snapshot(window_id, *, gate, timeout_s=30.0):
        assert window_id == 123
        assert gate is not None
        return {
            "success": True,
            "tree_markdown": "ok",
            "tree_quality": "poor",
            "node_count": 1,
        }

    monkeypatch.setattr(
        window_state_service,
        "get_accessibility_tree_snapshot",
        fake_get_accessibility_tree_snapshot,
    )

    result = await create_mcp_server().call_tool(
        "computer_get_accessibility_tree",
        {"window_id": 123},
    )

    assert result.structured_content == {
        "success": True,
        "tree_markdown": "ok",
        "tree_quality": "poor",
        "node_count": 1,
    }


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
            "pid": 123,
            "window_id": 456,
            "element_index": 1,
            "value": "abc",
        },
    )

    assert result.structured_content["success"] is True
    assert result.structured_content["method"] == "click_type"
