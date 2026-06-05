"""Integration tests for the current local HTTP MCP surface."""

from __future__ import annotations

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
