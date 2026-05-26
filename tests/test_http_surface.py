from fastapi.routing import APIWebSocketRoute

from computer_server.main import app


def test_legacy_websocket_api_removed():
    websocket_paths = {route.path for route in app.routes if isinstance(route, APIWebSocketRoute)}

    assert "/ws" not in websocket_paths


def test_legacy_commands_api_removed():
    http_paths = {getattr(route, "path", None) for route in app.routes}

    assert "/commands" not in http_paths
