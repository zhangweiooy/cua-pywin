from __future__ import annotations

import pytest

from computer_server.tool_cli import (
    DoctorCheck,
    _call_result_to_json,
    _collect_doctor_checks,
    _doctor_summary,
    _parse_tool_arguments,
    _rewrite_implicit_call,
    _tool_to_json,
    build_parser,
)


def test_rewrite_implicit_call() -> None:
    assert _rewrite_implicit_call(["computer_get_screen_size"]) == [
        "call",
        "computer_get_screen_size",
    ]
    assert _rewrite_implicit_call(["list-tools"]) == ["list-tools"]
    assert _rewrite_implicit_call(["doctor"]) == ["doctor"]


def test_parse_tool_arguments_requires_object() -> None:
    assert _parse_tool_arguments(None) == {}
    assert _parse_tool_arguments('{"x": 1}') == {"x": 1}

    with pytest.raises(SystemExit, match="tool arguments must be a JSON object"):
        _parse_tool_arguments("[1, 2, 3]")


def test_call_result_to_json() -> None:
    class Result:
        is_error = False
        structured_content = {"success": True}
        content = []

    assert _call_result_to_json(Result()) == {
        "is_error": False,
        "structured_content": {"success": True},
        "content": [],
    }


def test_tool_to_json_exposes_stable_schema_fields() -> None:
    class Tool:
        name = "computer_example"
        description = "Example tool"
        parameters = {"type": "object"}
        output_schema = None
        annotations = None
        fn = lambda: None

    assert _tool_to_json(Tool()) == {
        "name": "computer_example",
        "description": "Example tool",
        "inputSchema": {"type": "object"},
    }


def test_serve_accepts_http_server_options() -> None:
    args = build_parser().parse_args(
        [
            "serve",
            "--host",
            "0.0.0.0",
            "--port",
            "8443",
            "--log-level",
            "debug",
            "--ssl-keyfile",
            "key.pem",
            "--ssl-certfile",
            "cert.pem",
            "--i-know-this-exposes-my-computer",
            "--no-cli-rpc",
        ]
    )

    assert args.host == "0.0.0.0"
    assert args.port == 8443
    assert args.log_level == "debug"
    assert args.ssl_keyfile == "key.pem"
    assert args.ssl_certfile == "cert.pem"
    assert args.i_know_this_exposes_my_computer is True
    assert args.no_cli_rpc is True


def test_doctor_parser_accepts_options() -> None:
    args = build_parser().parse_args(
        [
            "doctor",
            "--json",
            "--timeout",
            "0.5",
            "--no-dependencies",
            "--no-daemon",
        ]
    )

    assert args.command == "doctor"
    assert args.json is True
    assert args.timeout == 0.5
    assert args.no_dependencies is True
    assert args.no_daemon is True


def test_doctor_summary_counts_statuses() -> None:
    assert _doctor_summary(
        [
            DoctorCheck("a", "ok", "ok"),
            DoctorCheck("b", "warn", "warn"),
            DoctorCheck("c", "fail", "fail"),
        ]
    ) == {"ok": 1, "warn": 1, "fail": 1}


def test_collect_doctor_checks_can_skip_slow_sections() -> None:
    payload = _collect_doctor_checks(dependencies=False, daemon=False)

    names = {check["name"] for check in payload["checks"]}
    assert {"python", "platform", "state_dir", "model:omniparser_yolo", "model:tinyclip"}.issubset(names)
    assert "daemon" not in names
    assert not any(name.startswith("dependency:") for name in names)
