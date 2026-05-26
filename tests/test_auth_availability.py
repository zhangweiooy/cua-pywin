"""Integration tests for the current local HTTP MCP surface.

The old authenticated HTTP command API was removed.  ``main.py`` now exposes
only a local status endpoint plus the MCP HTTP transport mounted at ``/mcp``.
"""

from __future__ import annotations

from fastapi.routing import APIWebSocketRoute
from fastapi.testclient import TestClient

from computer_server.main import app


def test_status_reports_mcp_only_http_surface():
    client = TestClient(app)

    resp = client.get("/status")

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["os_type"] == "windows"
    assert data["mcp_endpoint"] == "/mcp"
    assert data["http_api"] == "mcp_only"
    assert "Legacy /ws and /commands APIs have been removed" in data["note"]


def test_legacy_http_command_endpoints_removed():
    client = TestClient(app)

    for path in ("/cmd", "/commands", "/playwright_exec", "/pty/999999"):
        resp = client.post(path, json={"command": "version", "params": {}})
        if resp.status_code == 405:
            resp = client.get(path)
        assert resp.status_code == 404


def test_legacy_websocket_endpoint_removed_from_routes():
    websocket_paths = {
        route.path
        for route in app.routes
        if isinstance(route, APIWebSocketRoute)
    }

    assert "/ws" not in websocket_paths


def test_cors_allows_localhost_origins_only():
    client = TestClient(app)
    headers = {
        "Origin": "http://localhost:3000",
        "Access-Control-Request-Method": "GET",
    }

    resp = client.options("/status", headers=headers)

    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == "http://localhost:3000"


def test_cors_rejects_non_localhost_origins():
    client = TestClient(app)
    headers = {
        "Origin": "https://example.com",
        "Access-Control-Request-Method": "GET",
    }

    resp = client.options("/status", headers=headers)

    assert "access-control-allow-origin" not in resp.headers
