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

    async def get_application_windows(self, app: str):
        assert app == "Target"
        return {
            "success": True,
            "windows": [
                {"id": 456, "window_id": 456, "title": "Target Window"},
            ],
        }


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
async def test_get_app_windows_returns_canonical_window(monkeypatch):
    import computer_server.mcp_server as mcp_module

    monkeypatch.setattr(
        mcp_module,
        "_handlers",
        SimpleNamespace(window=_FakeWindow()),
    )

    result = await create_mcp_server().call_tool(
        "computer_get_app_windows",
        {"app": "Target"},
    )

    window_record = result.structured_content["windows"][0]
    assert window_record["id"] == 456
    assert window_record["window_id"] == 456
    assert window_record["window"]["id"] == 456
    assert window_record["window"]["window_id"] == 456
    assert window_record["window"]["title"] == "Target Window"


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

    result = await create_mcp_server().call_tool(
        "computer_get_window_state",
        {"window": {"id": 456, "pid": 123, "title": "Target"}},
    )

    content = result.structured_content
    assert content["success"] is True
    assert content["snapshot_id"].startswith("snap-")
    assert content["window"]["id"] == 456
    assert content["window"]["window_id"] == 456
    assert content["window"]["pid"] == 123
    assert content["window_ref"]["version"] == 1
    assert content["window_ref"]["pid"] == 123
    assert content["window_ref"]["window_id"] == 456


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

    assert {
        key: result.structured_content[key]
        for key in ("success", "tree_markdown", "tree_quality", "node_count")
    } == {
        "success": True,
        "tree_markdown": "ok",
        "tree_quality": "poor",
        "node_count": 1,
    }
    assert result.structured_content["window"]["id"] == 123
    assert result.structured_content["window"]["window_id"] == 123


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
