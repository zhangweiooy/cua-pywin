"""Integration tests for the current local HTTP MCP surface."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

import computer_server.main as main_module
import computer_server.overlay as overlay
from computer_server.main import app
from computer_server.tool_rpc import AUTH_HEADER, BATCH_HEADER, DAEMON_TOKEN_ENV


def test_status_reports_mcp_only_http_surface():
    client = TestClient(app)

    resp = client.get("/status")

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["os_type"] == "windows"
    assert data["mcp_endpoint"] == "/mcp"
    assert data["http_api"] == "mcp_only"
    assert data["note"] == "Use MCP at /mcp."


def test_http_surface_routes_are_current():
    routes = {
        route.path: sorted(getattr(route, "methods", []) or [])
        for route in app.routes
    }

    assert routes == {
        "/openapi.json": ["GET", "HEAD"],
        "/docs": ["GET", "HEAD"],
        "/docs/oauth2-redirect": ["GET", "HEAD"],
        "/redoc": ["GET", "HEAD"],
        "/status": ["GET"],
        "/tools": ["GET"],
        "/tools/{tool_name}": ["GET"],
        "/call": ["POST"],
        "/shutdown": ["POST"],
    }


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


def test_batch_call_suppresses_cursor_animation(monkeypatch):
    observed = []

    class FakeMcp:
        async def call_tool(self, name, arguments):
            observed.append(overlay.is_animation_suppressed())
            return SimpleNamespace(
                is_error=False,
                structured_content={"success": True},
                content=[],
            )

    monkeypatch.setenv(DAEMON_TOKEN_ENV, "secret")
    monkeypatch.setattr(main_module, "_get_mcp_server", lambda: FakeMcp())
    client = TestClient(app)

    response = client.post(
        "/call",
        headers={AUTH_HEADER: "secret", BATCH_HEADER: "1"},
        json={"name": "computer_get_screen_size", "arguments": {}},
    )

    assert response.status_code == 200
    assert observed == [True]
    assert overlay.is_animation_suppressed() is False
