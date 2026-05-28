from fastapi.routing import APIWebSocketRoute

from computer_server.main import app


def test_legacy_websocket_api_removed():
    websocket_paths = {route.path for route in app.routes if isinstance(route, APIWebSocketRoute)}

    assert "/ws" not in websocket_paths


def test_legacy_commands_api_removed():
    http_paths = {getattr(route, "path", None) for route in app.routes}

    assert "/commands" not in http_paths


def test_cli_json_rpc_routes_share_http_app():
    http_paths = {getattr(route, "path", None) for route in app.routes}

    assert "/tools" in http_paths
    assert "/tools/{tool_name}" in http_paths
    assert "/call" in http_paths
    assert "/shutdown" in http_paths
