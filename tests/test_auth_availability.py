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
    assert data["version"] == "0.2.6"
    assert data["protocol_version"] == "0.2"
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


def test_call_rejects_non_object_request_shapes_without_dispatch(monkeypatch):
    calls = []

    class FakeMcp:
        async def call_tool(self, name, arguments):
            calls.append((name, arguments))
            return SimpleNamespace(
                is_error=False,
                structured_content={"success": True},
                content=[],
            )

    monkeypatch.setenv(DAEMON_TOKEN_ENV, "secret")
    monkeypatch.setattr(main_module, "_get_mcp_server", lambda: FakeMcp())
    client = TestClient(app)
    headers = {AUTH_HEADER: "secret"}

    body_response = client.post("/call", headers=headers, json=[])
    arguments_response = client.post(
        "/call",
        headers=headers,
        json={"name": "computer_release_all_inputs", "arguments": []},
    )

    assert body_response.status_code == 200
    assert body_response.json()["transport_error"] == {
        "code": "INVALID_REQUEST",
        "message": "request body must be an object",
        "retry_safety": "safe",
    }
    assert arguments_response.status_code == 200
    assert arguments_response.json()["transport_error"] == {
        "code": "INVALID_REQUEST",
        "message": "arguments must be an object",
        "retry_safety": "safe",
    }
    assert calls == []


def test_call_malformed_json_uses_fixed_transport_wrapper(monkeypatch):
    monkeypatch.setenv(DAEMON_TOKEN_ENV, "secret")
    client = TestClient(app)

    response = client.post(
        "/call",
        headers={
            AUTH_HEADER: "secret",
            "Content-Type": "application/json",
        },
        content="{bad",
    )

    assert response.status_code == 200
    assert response.json() == {
        "protocol_version": "0.2",
        "result": None,
        "content": [],
        "transport_error": {
            "code": "INVALID_REQUEST",
            "message": "request body is not valid JSON",
            "retry_safety": "safe",
        },
    }


def test_call_auth_failures_use_fixed_transport_wrapper(monkeypatch):
    client = TestClient(app)
    request = {
        "name": "computer_get_screen_size",
        "arguments": {},
    }

    monkeypatch.delenv(DAEMON_TOKEN_ENV, raising=False)
    disabled = client.post("/call", json=request)

    monkeypatch.setenv(DAEMON_TOKEN_ENV, "secret")
    denied = client.post(
        "/call",
        headers={AUTH_HEADER: "wrong"},
        json=request,
    )

    assert disabled.status_code == 200
    assert disabled.json() == {
        "protocol_version": "0.2",
        "result": None,
        "content": [],
        "transport_error": {
            "code": "TRANSPORT_ERROR",
            "message": "CLI JSON RPC is not enabled",
            "retry_safety": "do_not_retry",
        },
    }
    assert denied.status_code == 200
    assert denied.json() == {
        "protocol_version": "0.2",
        "result": None,
        "content": [],
        "transport_error": {
            "code": "PERMISSION_DENIED",
            "message": "missing or invalid daemon token",
            "retry_safety": "do_not_retry",
        },
    }


def test_call_mcp_unavailable_uses_fixed_transport_wrapper(monkeypatch):
    monkeypatch.setenv(DAEMON_TOKEN_ENV, "secret")
    monkeypatch.setattr(main_module, "_get_mcp_server", lambda: None)
    client = TestClient(app)

    response = client.post(
        "/call",
        headers={AUTH_HEADER: "secret"},
        json={
            "name": "computer_get_screen_size",
            "arguments": {},
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "protocol_version": "0.2",
        "result": None,
        "content": [],
        "transport_error": {
            "code": "TRANSPORT_ERROR",
            "message": "MCP server unavailable",
            "retry_safety": "reobserve",
        },
    }
