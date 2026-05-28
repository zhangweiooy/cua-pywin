from __future__ import annotations

import os

os.environ["CUA_SKIP_SOM_WARMUP"] = "1"

import pytest

from computer_server.mcp_server import create_mcp_server


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
