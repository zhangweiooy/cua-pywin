from computer_server.main import app


def test_cli_json_rpc_routes_share_http_app():
    http_paths = {getattr(route, "path", None) for route in app.routes}

    assert "/tools" in http_paths
    assert "/tools/{tool_name}" in http_paths
    assert "/call" in http_paths
    assert "/shutdown" in http_paths
