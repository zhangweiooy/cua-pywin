from __future__ import annotations

import pytest

from computer_server.handlers.browser_cdp import CDPClient


@pytest.mark.asyncio
async def test_evaluate_retry_uses_resolved_target_id_not_tab_index(monkeypatch):
    client = CDPClient()
    sent_urls: list[str] = []

    async def fake_discover(port, host="localhost", force_refresh=False):
        if force_refresh:
            return [
                {"id": "tab-b", "webSocketDebuggerUrl": "ws://tab-b"},
                {"id": "tab-a", "webSocketDebuggerUrl": "ws://tab-a-new"},
            ]
        return [
            {"id": "tab-a", "webSocketDebuggerUrl": "ws://tab-a-old"},
            {"id": "tab-b", "webSocketDebuggerUrl": "ws://tab-b"},
        ]

    async def fake_send_command(ws_url, method, params, timeout=5.0):
        sent_urls.append(ws_url)
        if len(sent_urls) == 1:
            return {"error": "target closed"}
        return {"result": {"result": {"value": "ok"}}}

    monkeypatch.setattr(client, "discover", fake_discover)
    monkeypatch.setattr(client, "_send_command", fake_send_command)

    result = await client.evaluate(port=9222, expression="document.title", tab_index=0)

    assert result["error"] is None
    assert result["result"] == "ok"
    assert result["target_id"] == "tab-a"
    assert sent_urls == ["ws://tab-a-old", "ws://tab-a-new"]


@pytest.mark.asyncio
async def test_evaluate_target_id_ignores_tab_index(monkeypatch):
    client = CDPClient()
    sent_urls: list[str] = []

    async def fake_discover(port, host="localhost", force_refresh=False):
        return [
            {"id": "tab-a", "webSocketDebuggerUrl": "ws://tab-a"},
            {"id": "tab-b", "webSocketDebuggerUrl": "ws://tab-b"},
        ]

    async def fake_send_command(ws_url, method, params, timeout=5.0):
        sent_urls.append(ws_url)
        return {"result": {"result": {"value": "from-b"}}}

    monkeypatch.setattr(client, "discover", fake_discover)
    monkeypatch.setattr(client, "_send_command", fake_send_command)

    result = await client.evaluate(
        port=9222,
        expression="document.title",
        tab_index=0,
        target_id="tab-b",
    )

    assert result["error"] is None
    assert result["result"] == "from-b"
    assert result["target_id"] == "tab-b"
    assert sent_urls == ["ws://tab-b"]


@pytest.mark.asyncio
async def test_get_page_info_returns_target_id(monkeypatch):
    client = CDPClient()

    async def fake_discover(port, host="localhost", force_refresh=False):
        return [
            {
                "id": "tab-a",
                "title": "Cached title",
                "url": "https://example.test/",
                "webSocketDebuggerUrl": "ws://tab-a",
            }
        ]

    async def fake_send_command(ws_url, method, params, timeout=5.0):
        return {
            "result": {
                "result": {
                    "value": {
                        "title": "Live title",
                        "url": "https://example.test/page",
                        "domain": "example.test",
                    }
                }
            }
        }

    monkeypatch.setattr(client, "discover", fake_discover)
    monkeypatch.setattr(client, "_send_command", fake_send_command)

    info = await client.get_page_info(port=9222, tab_index=0)

    assert info == {
        "title": "Live title",
        "url": "https://example.test/page",
        "domain": "example.test",
        "target_id": "tab-a",
    }


@pytest.mark.asyncio
async def test_resolve_target_refreshes_when_target_id_misses_cache(monkeypatch):
    client = CDPClient()
    refresh_flags: list[bool] = []

    async def fake_discover(port, host="localhost", force_refresh=False):
        refresh_flags.append(force_refresh)
        if force_refresh:
            return [{"id": "tab-b", "webSocketDebuggerUrl": "ws://tab-b"}]
        return [{"id": "tab-a", "webSocketDebuggerUrl": "ws://tab-a"}]

    monkeypatch.setattr(client, "discover", fake_discover)

    target = await client._resolve_target(port=9222, target_id="tab-b")

    assert target == {"id": "tab-b", "webSocketDebuggerUrl": "ws://tab-b"}
    assert refresh_flags == [False, True]


@pytest.mark.asyncio
async def test_resolve_target_refreshes_when_tab_index_misses_cache(monkeypatch):
    client = CDPClient()
    refresh_flags: list[bool] = []

    async def fake_discover(port, host="localhost", force_refresh=False):
        refresh_flags.append(force_refresh)
        if force_refresh:
            return [
                {"id": "tab-a", "webSocketDebuggerUrl": "ws://tab-a"},
                {"id": "tab-b", "webSocketDebuggerUrl": "ws://tab-b"},
            ]
        return [{"id": "tab-a", "webSocketDebuggerUrl": "ws://tab-a"}]

    monkeypatch.setattr(client, "discover", fake_discover)

    target = await client._resolve_target(port=9222, tab_index=1)

    assert target == {"id": "tab-b", "webSocketDebuggerUrl": "ws://tab-b"}
    assert refresh_flags == [False, True]
